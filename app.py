import logging
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from flask_apscheduler import APScheduler
from flask_migrate import Migrate
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from scraper import Scraper
from models import db, User, Vendor, PriceData, ScrapeLog, FederalPriceData
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
def fetch_federal_data():
    api_key = "r5rztCthY9srZ1aBSnuzNZwcREkSujGwmXcAQ5KW"
    url = f"https://api.eia.gov/v2/petroleum/pri/wfr/data/?frequency=weekly&data[0]=value&facets[series][]=W_EPD2F_PRS_SRI_DPG&sort[0][column]=period&sort[0][direction]=desc&offset=0&length=5000&api_key={api_key}"
    
    logger.info(f"Fetching federal data from URL: {url}")
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        
        logger.info(f"Received {len(data['response']['data'])} data points from the API")
        
        new_entries = 0
        updated_entries = 0
        for item in data['response']['data']:
            date = datetime.strptime(item['period'], "%Y-%m-%d").date()
            price = float(item['value'])
            
            existing_data = FederalPriceData.query.filter_by(date=date).first()
            if existing_data:
                if existing_data.price != price:
                    existing_data.price = price
                    updated_entries += 1
            else:
                new_data = FederalPriceData(date=date, price=price)
                db.session.add(new_data)
                new_entries += 1
        
        db.session.commit()
        logger.info(f"Federal price data updated successfully. New entries: {new_entries}, Updated entries: {updated_entries}")
        return True, f"Federal data updated. New: {new_entries}, Updated: {updated_entries}"
    except Exception as e:
        logger.error(f"Error fetching federal price data: {str(e)}", exc_info=True)
        return False, f"Error fetching federal data: {str(e)}"

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
            success, message = perform_scrape()
        elif 'federal_data' in request.form:
            success, message = fetch_federal_data()
        
        # Log the attempt for both local and federal data
        log_entry = ScrapeLog(
            timestamp=datetime.now(),
            success=success,
            message=message,
            scheduled=False,
            scrape_type='Local' if 'local_scrape' in request.form else 'Federal'
        )
        db.session.add(log_entry)
        db.session.commit()
        
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
            success, message = fetch_federal_data()
            log_entry = ScrapeLog(
                timestamp=datetime.now(),
                success=success,
                message=message,
                scheduled=True,
                scrape_type='Federal'
            )
            db.session.add(log_entry)
            db.session.commit()
            if success:
                logger.info("Scheduled federal data fetch completed successfully")
            else:
                logger.error("Scheduled federal data fetch failed")
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
    time_window = request.args.get('time_window', '90')  # Default to 90 days
    days_ago = datetime.now().date() - timedelta(days=int(time_window))

    # Query to get price data for all vendors within the specified time window
    query = (
        select(Vendor.name, PriceData.date, PriceData.price)
        .select_from(Vendor)
        .join(PriceData, Vendor.id == PriceData.vendor_id)
        .where(PriceData.date >= days_ago)
        .order_by(Vendor.name, PriceData.date)
    )

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

    # Fetch federal price data for the specified time window
    federal_data = FederalPriceData.query.filter(FederalPriceData.date >= days_ago).order_by(FederalPriceData.date).all()
    federal_prices = {
        'dates': [data.date.strftime('%Y-%m-%d') for data in federal_data],
        'prices': [float(data.price) for data in federal_data]
    }
    
    logger.info(f"Trends page accessed. Vendor data points: {sum(len(v['dates']) for v in vendors.values())}, Federal data points: {len(federal_data)}")

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'vendors': vendors, 'federal_prices': federal_prices})
    else:
        return render_template('trends.html', vendors=vendors, federal_prices=federal_prices)

if __name__ == '__main__':
    app.run(debug=True)