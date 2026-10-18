import json
import logging
import requests
from bisect import bisect_left
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from flask_apscheduler import APScheduler
from flask_migrate import Migrate
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from scraper import Scraper
from models import db, User, Vendor, PriceData, ScrapeLog, FederalPriceData, MarketPriceData, UserSettings
from config import Config
from sqlalchemy import func, select, and_, desc

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config.from_object(Config)

# Initialize extensions
db.init_app(app)
migrate = Migrate(app, db)

# The production DB was not created through Alembic, so create any missing
# tables (e.g. spot_price_data) at startup. Existing tables are left as-is.
with app.app_context():
    db.create_all()
login_manager = LoginManager(app)
login_manager.login_view = 'login'
scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()

# Create a single Scraper instance
scraper = Scraper()

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# Function to create a new user
def create_user(username, password):
    with app.app_context():
        user = User.query.filter_by(username=username).first()
        if user:
            logger.info(f"User {username} already exists")
            return
        
        new_user = User(username=username, password_hash=generate_password_hash(password))
        db.session.add(new_user)
        db.session.commit()
        logger.info(f"User {username} created successfully")

# Scraping function
def perform_scrape(scheduled=False):
    logger.info(f"{'Scheduled' if scheduled else 'Manual'} scrape initiated")
    try:
        scraper.scrape()
        success = True
        message = 'Scraping completed successfully.'
        logger.info(message)
    except Exception as e:
        success = False
        message = f"Error during scraping: {str(e)}"
        logger.error(message, exc_info=True)
    
    # Log the scrape attempt
    log_entry = ScrapeLog(
        timestamp=datetime.now(),
        success=success,
        message=message,
        scheduled=scheduled,
        scrape_type='Local'  # Add this line to set the scrape_type
    )
    db.session.add(log_entry)
    db.session.commit()
    
    return success, message

# Scheduled task to scrape local data
@scheduler.task('cron', id='daily_scrape', hour=10)
def scheduled_scrape():
    with app.app_context():
        perform_scrape(scheduled=True)

# Fetch data from the Fed's EIA site on oil prices
EIA_API_KEY = "r5rztCthY9srZ1aBSnuzNZwcREkSujGwmXcAQ5KW"
EIA_TIMEOUT_SECONDS = 300  # the EIA API can take minutes to return a full history
EIA_REFRESH_WEEKS = 8      # once a series is loaded, re-fetch only recent weeks (picks up EIA revisions)

# EIA series shown as trends indexes (stored in MarketPriceData)
NY_SPOT_SERIES = 'EER_EPD2F_PF4_Y35NY_DPG'          # NY Harbor No. 2 heating oil spot, FOB
DIESEL_LOCAL_SERIES = 'EMD_EPD2D_PTE_R1X_DPG'       # New England (PADD 1A) retail diesel; no RI series exists
DIESEL_SPOT_SERIES = 'EER_EPD2DXL0_PF4_Y35NY_DPG'   # NY Harbor ultra-low sulfur diesel spot

def fetch_eia_series(route, series_id, label, model=MarketPriceData):
    """Fetch a weekly EIA series and upsert it into a (date, price) model.

    MarketPriceData rows are scoped by series id; other models hold one series each.
    """
    scope = {'series': series_id} if model is MarketPriceData else {}
    latest = db.session.query(func.max(model.date)).filter_by(**scope).scalar()
    start = f"&start={(latest - timedelta(weeks=EIA_REFRESH_WEEKS)).isoformat()}" if latest else ""
    url = f"https://api.eia.gov/v2/petroleum/{route}/data/?frequency=weekly&data[0]=value&facets[series][]={series_id}{start}&sort[0][column]=period&sort[0][direction]=desc&offset=0&length=5000&api_key={EIA_API_KEY}"

    logger.info(f"Fetching {label} data for EIA series {series_id} ({'since ' + start[7:] if start else 'full history'})")
    try:
        # The EIA API intermittently returns 5xx gateway timeouts; retry once
        for attempt in range(2):
            response = requests.get(url, timeout=EIA_TIMEOUT_SECONDS)
            if response.status_code < 500 or attempt == 1:
                break
            logger.warning(f"EIA returned HTTP {response.status_code} for {label}; retrying")
        response.raise_for_status()
        data = response.json()

        logger.info(f"Received {len(data['response']['data'])} data points from the API")

        existing = {row.date: row for row in model.query.filter_by(**scope)}
        new_entries = 0
        updated_entries = 0
        for item in data['response']['data']:
            if item['value'] is None:
                continue
            date = datetime.strptime(item['period'], "%Y-%m-%d").date()
            price = float(item['value'])
            if price <= 0:
                continue  # $0 is a bad data point, never a real price

            existing_data = existing.get(date)
            if existing_data:
                if existing_data.price != price:
                    existing_data.price = price
                    updated_entries += 1
            else:
                new_data = model(date=date, price=price, **scope)
                db.session.add(new_data)
                existing[date] = new_data
                new_entries += 1

        db.session.commit()
        logger.info(f"{label} price data updated successfully. New entries: {new_entries}, Updated entries: {updated_entries}")
        return True, f"{label} data updated. New: {new_entries}, Updated: {updated_entries}"
    except Exception as e:
        db.session.rollback()
        # Error text from requests includes the request URL, so redact the API key
        error = str(e).replace(EIA_API_KEY, '***')
        logger.error(f"Error fetching {label} price data: {error}")
        return False, f"Error fetching {label} data: {error}"

def fetch_federal_data():
    # Rhode Island residential No. 2 heating oil (heating season only, Oct-Mar)
    return fetch_eia_series('pri/wfr', 'W_EPD2F_PRS_SRI_DPG', 'Federal', model=FederalPriceData)

def fetch_spot_data():
    return fetch_eia_series('pri/spt', NY_SPOT_SERIES, 'NY Spot')

def fetch_diesel_data():
    return fetch_eia_series('pri/gnd', DIESEL_LOCAL_SERIES, 'Local Diesel')

def fetch_diesel_spot_data():
    return fetch_eia_series('pri/spt', DIESEL_SPOT_SERIES, 'Diesel Spot')

# Year-round market series pulled together by the "Pull Market Data" button and the daily job
MARKET_FETCHES = [(fetch_spot_data, 'Spot'), (fetch_diesel_data, 'Diesel'), (fetch_diesel_spot_data, 'DieselSpot')]

def log_fetch(success, message, scheduled, scrape_type):
    db.session.add(ScrapeLog(
        timestamp=datetime.now(),
        success=success,
        message=message,
        scheduled=scheduled,
        scrape_type=scrape_type
    ))
    db.session.commit()

# Routes
@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            flash('Logged in successfully.', 'success')
            logger.info(f"User {username} logged in successfully")
            return redirect(url_for('index'))
        else:
            flash('Invalid username or password.', 'error')
            logger.warning(f"Failed login attempt for username: {username}")
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logger.info(f"User {current_user.username} logged out")
    logout_user()
    flash('Logged out successfully.', 'success')
    return redirect(url_for('login'))

@app.route('/scrape', methods=['GET', 'POST'])
@login_required
def scrape_page():
    if request.method == 'POST':
        if 'local_scrape' in request.form:
            # perform_scrape() writes its own ScrapeLog entry
            success, message = perform_scrape()
        elif 'federal_data' in request.form:
            success, message = fetch_federal_data()
            # fetch_federal_data() does not self-log, so record the attempt here
            log_fetch(success, message, scheduled=False, scrape_type='Federal')
        elif 'market_data' in request.form:
            results = []
            for fetch, scrape_type in MARKET_FETCHES:
                ok, msg = fetch()
                log_fetch(ok, msg, scheduled=False, scrape_type=scrape_type)
                results.append((ok, msg))
            success = all(ok for ok, _ in results)
            message = ' | '.join(msg for _, msg in results)

        flash(message, 'success' if success else 'error')
        return redirect(url_for('scrape_page'))
    
    logs = ScrapeLog.query.order_by(ScrapeLog.timestamp.desc()).all()
    local_job = scheduler.get_job('daily_scrape')
    federal_job = scheduler.get_job('fetch_federal_data')
    is_local_scheduled = local_job is not None and local_job.next_run_time is not None
    is_federal_scheduled = federal_job is not None and federal_job.next_run_time is not None
    
    return render_template('scrape.html', logs=logs, is_local_scheduled=is_local_scheduled, is_federal_scheduled=is_federal_scheduled)

@app.route('/toggle_schedule', methods=['POST'])
@login_required
def toggle_schedule():
    schedule_type = request.json.get('scheduleType')
    is_scheduled = request.json.get('isScheduled')
    
    if schedule_type == 'local':
        job_id = 'daily_scrape'
    elif schedule_type == 'federal':
        job_id = 'fetch_federal_data'
    else:
        return jsonify(success=False, message="Invalid schedule type"), 400
    
    if is_scheduled:
        scheduler.resume_job(job_id)
        logger.info(f"Scheduled {schedule_type} data scraping resumed")
    else:
        scheduler.pause_job(job_id)
        logger.info(f"Scheduled {schedule_type} data scraping paused")
    
    return jsonify(success=True)

# Modify the scheduled task for federal data to check if it's enabled
@scheduler.task('cron', id='fetch_federal_data', hour=6)
def scheduled_federal_data_fetch():
    with app.app_context():
        job = scheduler.get_job('fetch_federal_data')
        if job and job.next_run_time is not None:
            # The market series ride along with the federal job (same EIA API)
            for fetch, scrape_type in [(fetch_federal_data, 'Federal')] + MARKET_FETCHES:
                success, message = fetch()
                log_fetch(success, message, scheduled=True, scrape_type=scrape_type)
                if success:
                    logger.info(f"Scheduled {scrape_type} data fetch completed successfully")
                else:
                    logger.error(f"Scheduled {scrape_type} data fetch failed")
        else:
            logger.info("Scheduled federal data fetch skipped (disabled)")

@app.route('/prices')
@login_required
def prices():
    # Calculate the date 3 months ago from today
    three_months_ago = datetime.now().date() - timedelta(days=90)

    # Subquery to get the latest price for each vendor within the last 3 months
    latest_prices_subquery = (
        select(
            PriceData.vendor_id,
            func.max(PriceData.date).label('max_date')
        )
        .where(PriceData.date >= three_months_ago)
        .group_by(PriceData.vendor_id)
        .subquery()
    )

    # Query to get the latest prices for all vendors, sorted by price
    query = (
        select(Vendor, PriceData)
        .select_from(Vendor)
        .join(PriceData, Vendor.id == PriceData.vendor_id)
        .join(
            latest_prices_subquery,
            and_(
                Vendor.id == latest_prices_subquery.c.vendor_id,
                PriceData.date == latest_prices_subquery.c.max_date
            )
        )
        .order_by(PriceData.price)  # Sort by price, lowest to highest
    )

    latest_prices = db.session.execute(query).all()

    logger.info(f"Prices page accessed by user {current_user.username}")
    return render_template('prices.html', latest_prices=latest_prices)

@app.route('/trends')
@login_required
def trends():
    trends_settings = load_trends_settings()
    # Explicit query param (time window change) wins, then the saved setting, then 90 days; 'all' = no cutoff
    time_window = request.args.get('time_window') or trends_settings.get('timeWindow') or '90'
    if time_window == 'all':
        days_ago = None
    else:
        try:
            days = int(time_window)
        except ValueError:
            days = 90
        days_ago = datetime.now().date() - timedelta(days=days)

    # Query to get price data for all vendors within the specified time window
    query = (
        select(Vendor.name, PriceData.date, PriceData.price)
        .select_from(Vendor)
        .join(PriceData, Vendor.id == PriceData.vendor_id)
        .order_by(Vendor.name, PriceData.date)
    )
    if days_ago:
        query = query.where(PriceData.date >= days_ago)

    results = db.session.execute(query).fetchall()

    # Process the data for the chart
    vendors = {}
    for row in results:
        vendor_name = row.name
        if vendor_name not in vendors:
            vendors[vendor_name] = {'dates': [], 'prices': []}
        vendors[vendor_name]['dates'].append(row.date.strftime('%Y-%m-%dT08:00:00Z'))
        #vendors[vendor_name]['dates'].append(row.date.strftime('%Y-%m-%d'))
        vendors[vendor_name]['prices'].append(float(row.price))

    # Federal (raw + estimated), spread and spot series are built from full
    # history so estimates near the window edge use anchors outside the window
    federal_series, spread_series, spot_series = build_federal_index()

    # Local average (raw daily means + weekly estimates across gaps), built from
    # full history like the federal series so gap anchors outside the window are used
    local = [p for p in build_local_index(nearest_price_lookup(spot_series)) if not days_ago or p[0] >= days_ago]
    local_average = {
        'dates': [d.strftime('%Y-%m-%dT08:00:00Z') for d, _, _ in local],
        'prices': [price for _, price, _ in local],
        'estimated': [estimated for _, _, estimated in local]
    }

    def in_window(series):
        return [p for p in series if not days_ago or p[0] >= days_ago]

    federal = in_window(federal_series)
    federal_prices = {
        'dates': [d.strftime('%Y-%m-%d') for d, _, _ in federal],
        'prices': [price for _, price, _ in federal],
        'estimated': [estimated for _, _, estimated in federal]
    }
    spread = in_window(spread_series)
    price_spread = {
        'dates': [d.strftime('%Y-%m-%d') for d, _ in spread],
        'prices': [value for _, value in spread]
    }
    def to_chart(series):
        points = in_window(series)
        return {
            'dates': [d.strftime('%Y-%m-%d') for d, _ in points],
            'prices': [price for _, price in points]
        }

    spot_prices = to_chart(spot_series)
    diesel_series = market_series(DIESEL_LOCAL_SERIES)
    diesel_spot_series = market_series(DIESEL_SPOT_SERIES)
    diesel_prices = to_chart(diesel_series)
    diesel_spot_prices = to_chart(diesel_spot_series)
    # NE retail diesel dates (Mondays) pair with the nearest NY spot date (Fridays)
    diesel_spread = to_chart(compute_spread(diesel_series, nearest_price_lookup(diesel_spot_series)))

    logger.info(f"Trends page accessed. Vendor data points: {sum(len(v['dates']) for v in vendors.values())}, Federal data points: {len(federal)} ({sum(federal_prices['estimated'])} estimated), Spot data points: {len(spot_prices['dates'])}")

    chart_data = {'vendors': vendors, 'federal_prices': federal_prices, 'local_average': local_average,
                  'spot_prices': spot_prices, 'price_spread': price_spread,
                  'diesel_prices': diesel_prices, 'diesel_spot_prices': diesel_spot_prices,
                  'diesel_spread': diesel_spread}
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(chart_data)
    else:
        return render_template('trends.html', trends_settings=trends_settings, **chart_data)

TIME_WINDOWS = {'30', '90', '365', '1095', 'all'}
VENDOR_SELECTIONS = {'top5', 'top10', 'all', 'none', 'favorites'}
INDEXES = ('federal', 'local', 'spot', 'spread', 'diesel', 'dieselSpot', 'dieselSpread')

def load_trends_settings():
    row = db.session.get(UserSettings, current_user.id)
    if not row:
        return {}
    try:
        return json.loads(row.settings).get('trends', {})
    except ValueError:
        return {}

def clean_trends_settings(data):
    """Keep only known keys with valid values from a client-supplied settings dict."""
    def vendor_names(value):
        if not isinstance(value, list):
            return []
        return [v for v in value if isinstance(v, str) and len(v) <= 100][:200]

    indexes = data.get('indexes') if isinstance(data.get('indexes'), dict) else {}
    cleaned = {
        'indexes': {name: bool(indexes.get(name)) for name in INDEXES},
        'favorites': vendor_names(data.get('favorites')),
        'hiddenVendors': vendor_names(data.get('hiddenVendors')),
    }
    if data.get('timeWindow') in TIME_WINDOWS:
        cleaned['timeWindow'] = data['timeWindow']
    if data.get('vendorSelection') in VENDOR_SELECTIONS:
        cleaned['vendorSelection'] = data['vendorSelection']
    return cleaned

@app.route('/trends/settings', methods=['POST'])
@login_required
def save_trends_settings():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(success=False, message="Invalid settings"), 400

    row = db.session.get(UserSettings, current_user.id)
    if not row:
        row = UserSettings(user_id=current_user.id, settings='{}')
        db.session.add(row)
    try:
        all_settings = json.loads(row.settings or '{}')
    except ValueError:
        all_settings = {}
    all_settings['trends'] = clean_trends_settings(data)
    row.settings = json.dumps(all_settings)
    db.session.commit()
    return jsonify(success=True)

def market_series(series_id):
    """[(date, price)] for one MarketPriceData series, oldest first."""
    rows = MarketPriceData.query.filter_by(series=series_id).order_by(MarketPriceData.date)
    return [(r.date, float(r.price)) for r in rows]

FEDERAL_GAP_DAYS = 10   # federal points further apart than this are a gap to estimate
SPOT_MATCH_DAYS = 6      # federal dates (Mondays) pair with the nearest spot date (Fridays)

def nearest_price_lookup(series):
    """Return f(day) -> price of the series point nearest to day (within SPOT_MATCH_DAYS), or None."""
    dates = [d for d, _ in series]

    def nearest(day):
        i = bisect_left(dates, day)
        candidates = [series[j] for j in (i - 1, i) if 0 <= j < len(series)]
        candidates = [c for c in candidates if abs((c[0] - day).days) <= SPOT_MATCH_DAYS]
        if not candidates:
            return None
        return min(candidates, key=lambda c: abs((c[0] - day).days))[1]
    return nearest

def compute_spread(retail, spot_near):
    """[(date, retail - spot)] on each retail date that has a nearby spot price."""
    spread = []
    for day, price in retail:
        spot_price = spot_near(day)
        if spot_price is not None:
            spread.append((day, round(price - spot_price, 3)))
    return spread

LOCAL_GAP_DAYS = 10      # local average points further apart than this are a gap to estimate
LOCAL_ANCHOR_DAYS = 14   # average the local-vs-spot spread over this many days at each gap edge

def build_local_index(spot_near):
    """Return [(date, price, estimated)]: the daily mean of all vendor prices,
    plus weekly estimates filling gaps longer than LOCAL_GAP_DAYS.

    Estimate = NY spot price + spread, where the spread (local - spot) is
    averaged over LOCAL_ANCHOR_DAYS on each side of the gap (daily local
    averages are noisy) and interpolated linearly across it.
    """
    rows = (db.session.query(PriceData.date, func.avg(PriceData.price))
            .group_by(PriceData.date).order_by(PriceData.date).all())
    local = [(day, round(float(avg), 3)) for day, avg in rows]
    spreads = compute_spread(local, spot_near)
    spread_dates = [d for d, _ in spreads]

    def mean_spread(start, end):
        window = spreads[bisect_left(spread_dates, start):bisect_left(spread_dates, end + timedelta(days=1))]
        return sum(s for _, s in window) / len(window) if window else None

    estimates = []
    for (d0, _), (d1, _) in zip(local, local[1:]):
        span = (d1 - d0).days
        if span <= LOCAL_GAP_DAYS:
            continue
        s0 = mean_spread(d0 - timedelta(days=LOCAL_ANCHOR_DAYS), d0)
        s1 = mean_spread(d1, d1 + timedelta(days=LOCAL_ANCHOR_DAYS))
        if s0 is None or s1 is None:
            continue
        day = d0 + timedelta(days=7)
        while (d1 - day).days >= 4:
            spot_price = spot_near(day)
            if spot_price is not None:
                spread = s0 + (s1 - s0) * (day - d0).days / span
                estimates.append((day, round(spot_price + spread, 3), True))
            day += timedelta(days=7)

    return sorted([(d, p, False) for d, p in local] + estimates)

def build_federal_index():
    """Return (federal, spread, spot) series over the full history.

    federal: [(date, price, estimated)] - raw EIA points plus weekly estimates
             filling the gaps. Estimate = spot price + spread, where the spread
             (federal - spot) is interpolated linearly between the raw points on
             either side of the gap, or held at the last known spread after the
             most recent raw point.
    spread:  [(date, federal - spot)] on raw federal dates only.
    spot:    [(date, price)]
    """
    federal = [(r.date, float(r.price)) for r in FederalPriceData.query.order_by(FederalPriceData.date)]
    spot = market_series(NY_SPOT_SERIES)
    spot_dates = [d for d, _ in spot]
    spot_near = nearest_price_lookup(spot)
    spread = dict(compute_spread(federal, spot_near))

    estimates = []

    def estimate_weeks(start, end, spread_at):
        """Add weekly estimates after `start` up to `end`, using spread_at(day)."""
        day = start + timedelta(days=7)
        while (end - day).days >= 4:
            spot_price = spot_near(day)
            if spot_price is not None:
                estimates.append((day, round(spot_price + spread_at(day), 3), True))
            day += timedelta(days=7)

    for (d0, _), (d1, _) in zip(federal, federal[1:]):
        if (d1 - d0).days > FEDERAL_GAP_DAYS and d0 in spread and d1 in spread:
            s0, s1 = spread[d0], spread[d1]
            span = (d1 - d0).days
            estimate_weeks(d0, d1, lambda day: s0 + (s1 - s0) * (day - d0).days / span)

    # Trailing gap: carry the last known spread forward to the latest spot week
    if federal and spot and federal[-1][0] in spread:
        last_day = federal[-1][0]
        last_spread = spread[last_day]
        estimate_weeks(last_day, spot_dates[-1] + timedelta(days=SPOT_MATCH_DAYS), lambda day: last_spread)

    federal_series = sorted([(d, p, False) for d, p in federal] + estimates)
    return federal_series, sorted(spread.items()), spot

if __name__ == '__main__':
    app.run(debug=True)