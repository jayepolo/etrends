import requests
from bs4 import BeautifulSoup
from datetime import datetime
import logging
from models import db, Vendor, PriceData

class Scraper:
    def __init__(self):
        self.url = "https://www.newenglandoil.com/rhodeisland/zone4.asp?x=0"
        self.logger = logging.getLogger(__name__)

    def parse_date(self, date_str):
        if not date_str or date_str == '\xa0':
            return None
        try:
            # Remove any non-breaking spaces and strip whitespace
            date_str = date_str.replace('\xa0', '').strip()
            return datetime.strptime(date_str, '%m/%d/%Y').date()
        except ValueError:
            self.logger.warning(f"Could not parse date: {date_str}")
            return None

    def parse_price(self, price_str):
        if not price_str or price_str == '\xa0':
            return None
        try:
            return float(price_str.replace('$', '').replace(',', ''))
        except ValueError:
            self.logger.warning(f"Could not parse price: {price_str}")
            return None

    def scrape(self):
        self.logger.info(f"Starting scrape from URL: {self.url}")
        response = requests.get(self.url)
        self.logger.info(f"Request status code: {response.status_code}")
        
        soup = BeautifulSoup(response.content, 'html.parser')
        self.logger.info("Successfully parsed HTML content")
        
        table = soup.find('table')
        if not table:
            self.logger.error("Could not find the price table")
            raise Exception("Could not find the oil prices table")

        rows = table.find_all('tr')[1:]  # Skip the header row
        self.logger.info(f"Found {len(rows)} data rows")
        
        for i, row in enumerate(rows, start=1):
            columns = row.find_all('td')
            if len(columns) >= 6:
                company_name = columns[0].text.strip()
                town = columns[1].text.strip()
                price = self.parse_price(columns[2].text.strip())
                date_cell = columns[4]  # The date is in the 5th column (index 4)
                date_str = date_cell.text.strip()
                date = self.parse_date(date_str)

                self.logger.info(f"Processing row {i}: Company: {company_name}, Town: {town}, Price: {price}, Raw Date: '{date_str}', Parsed Date: {date}")

                if price is not None and price <= 0:
                    # A $0 (or negative) price is a bad listing, never a real price
                    self.logger.warning(f"Skipping row {i} due to invalid price {price} for {company_name}")
                elif price is not None and date is not None:
                    # Check if vendor exists, if not create a new one
                    vendor = Vendor.query.filter_by(name=company_name).first()
                    if not vendor:
                        vendor = Vendor(name=company_name, town=town)
                        db.session.add(vendor)
                        db.session.commit()
                        self.logger.info(f"Created new vendor: {company_name}")

                    # Check if price data already exists for this vendor and date
                    existing_price = PriceData.query.filter_by(vendor_id=vendor.id, date=date).first()
                    if existing_price:
                        self.logger.info(f"Price data already exists for {company_name} on {date}. Updating.")
                        existing_price.price = price
                    else:
                        # Add new price data
                        price_data = PriceData(vendor_id=vendor.id, price=price, date=date)
                        db.session.add(price_data)
                        self.logger.info(f"Added new price data for {company_name}: {price} on {date}")
                else:
                    self.logger.warning(f"Skipping row {i} due to missing price or date. Price: {price}, Date: {date}")

        db.session.commit()
        self.logger.info("Successfully committed all changes to database")
        self.logger.info("Scraping completed successfully")