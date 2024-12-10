from datetime import datetime, timedelta
from functools import lru_cache
import re
from typing import Dict, Tuple, Optional, List
from dateutil.relativedelta import relativedelta
from dateutil.parser import parse
import calendar

class DateRangeHandler:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DateRangeHandler, cls).__new__(cls)
            cls._instance.initialize()
        return cls._instance
    
    def initialize(self):
        """Initialize the handler with default values and patterns"""
        self.cache = {}
        self.cache_max_size = 1000  # Add cache size limit
        self.default_range = {
            "start_date": datetime(datetime.now().year, 1, 1).strftime('%Y-%m-%d'),
            "end_date": datetime.now().strftime('%Y-%m-%d')
        }
        
        self.date_patterns = {
            r'last (\d+) days?': self._get_last_n_days,
            r'last (\d+) weeks?': self._get_last_n_weeks,
            r'last (\d+) months?': self._get_last_n_months,
            r'last (\d+) quarters?': self._get_last_n_quarters,
            r'last (\d+) years?': self._get_last_n_years,
            r'this (week|month|quarter|year)': self._get_current_period,
            r'previous (week|month|quarter|year)': self._get_previous_period,
            r'year to date': self._get_year_to_date,
            r'quarter to date': self._get_quarter_to_date,
            r'month to date': self._get_month_to_date,
            r'(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})': self._get_custom_range
        }

    def get_date_range(self, query: str = None) -> Dict[str, str]:
        """Main interface method to get date range"""
        if not query:
            return self.default_range
            
        query = query.lower().strip()
        
        # Check cache first
        if query in self.cache:
            return self.cache[query]
            
        # Try to extract date range from query
        date_range = self._parse_date_range(query)
        
        # Validate the date range
        if self._validate_date_range(date_range):
            self.cache[query] = date_range
            return date_range
            
        return self.default_range

    @lru_cache(maxsize=100)
    def _parse_date_range(self, query: str) -> Dict[str, str]:
        """Parse date range from query using patterns"""
        for pattern, handler in self.date_patterns.items():
            match = re.search(pattern, query)
            if match:
                try:
                    if len(match.groups()) == 1:
                        return handler(match.group(1))
                    elif len(match.groups()) == 2:
                        return handler(match.group(1), match.group(2))
                except Exception:
                    continue
                    
        return self.default_range

    def _validate_date_range(self, date_range: Dict[str, str]) -> bool:
        """Validate date range format and logic"""
        try:
            start_date = datetime.strptime(date_range["start_date"], '%Y-%m-%d')
            end_date = datetime.strptime(date_range["end_date"], '%Y-%m-%d')
            
            # Check if dates are valid
            if start_date > datetime.now() or end_date > datetime.now():
                return False
                
            # Check if start date is before end date
            if start_date > end_date:
                return False
                
            # Check if date range is not too long (e.g., max 2 years)
            if (end_date - start_date).days > 730:  # 2 years
                return False
                
            return True
            
        except (ValueError, KeyError):
            return False

    def _get_last_n_days(self, n: str) -> Dict[str, str]:
        """Get date range for last n days"""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=int(n))
        return self._format_date_range(start_date, end_date)

    def _get_last_n_weeks(self, n: str) -> Dict[str, str]:
        """Get date range for last n weeks"""
        end_date = datetime.now()
        start_date = end_date - timedelta(weeks=int(n))
        return self._format_date_range(start_date, end_date)

    def _get_last_n_months(self, n: str) -> Dict[str, str]:
        """Get date range for last n months"""
        end_date = datetime.now()
        start_date = end_date - relativedelta(months=int(n))
        return self._format_date_range(start_date, end_date)

    def _get_last_n_quarters(self, n: str) -> Dict[str, str]:
        """Get date range for last n quarters"""
        end_date = datetime.now()
        start_date = end_date - relativedelta(months=int(n) * 3)
        return self._format_date_range(start_date, end_date)

    def _get_last_n_years(self, n: str) -> Dict[str, str]:
        """Get date range for last n years"""
        end_date = datetime.now()
        start_date = end_date - relativedelta(years=int(n))
        return self._format_date_range(start_date, end_date)

    def _get_current_period(self, period: str) -> Dict[str, str]:
        """Get date range for current period"""
        end_date = datetime.now()
        
        if period == "week":
            start_date = end_date - timedelta(days=end_date.weekday())
        elif period == "month":
            start_date = end_date.replace(day=1)
        elif period == "quarter":
            current_quarter = (end_date.month - 1) // 3
            start_date = datetime(end_date.year, current_quarter * 3 + 1, 1)
        else:  # year
            start_date = datetime(end_date.year, 1, 1)
            
        return self._format_date_range(start_date, end_date)

    def _get_previous_period(self, period: str) -> Dict[str, str]:
        """Get date range for previous period"""
        now = datetime.now()
        
        if period == "week":
            end_date = now - timedelta(days=now.weekday() + 1)
            start_date = end_date - timedelta(days=6)
        elif period == "month":
            end_date = now.replace(day=1) - timedelta(days=1)
            start_date = end_date.replace(day=1)
        elif period == "quarter":
            current_quarter = (now.month - 1) // 3
            if current_quarter == 0:
                start_date = datetime(now.year - 1, 10, 1)
                end_date = datetime(now.year - 1, 12, 31)
            else:
                start_date = datetime(now.year, (current_quarter - 1) * 3 + 1, 1)
                end_date = datetime(now.year, current_quarter * 3, 1) - timedelta(days=1)
        else:  # year
            start_date = datetime(now.year - 1, 1, 1)
            end_date = datetime(now.year - 1, 12, 31)
            
        return self._format_date_range(start_date, end_date)

    def _get_custom_range(self, start_date: str, end_date: str) -> Dict[str, str]:
        """Get custom date range"""
        try:
            start = datetime.strptime(start_date, '%Y-%m-%d')
            end = datetime.strptime(end_date, '%Y-%m-%d')
            return self._format_date_range(start, end)
        except ValueError:
            return self.default_range

    def _format_date_range(self, start_date: datetime, end_date: datetime) -> Dict[str, str]:
        """Format dates to string format"""
        return {
            "start_date": start_date.strftime('%Y-%m-%d'),
            "end_date": end_date.strftime('%Y-%m-%d')
        }

    def get_comparable_period(self, date_range: Dict[str, str], comparison_type: str) -> Dict[str, str]:
        """Get comparable period for given date range"""
        start_date = datetime.strptime(date_range["start_date"], '%Y-%m-%d')
        end_date = datetime.strptime(date_range["end_date"], '%Y-%m-%d')
        
        if comparison_type == "yoy":
            start_date = start_date - relativedelta(years=1)
            end_date = end_date - relativedelta(years=1)
        elif comparison_type == "mom":
            start_date = start_date - relativedelta(months=1)
            end_date = end_date - relativedelta(months=1)
        elif comparison_type == "wow":
            start_date = start_date - timedelta(weeks=1)
            end_date = end_date - timedelta(weeks=1)
        
        return self._format_date_range(start_date, end_date)

    def _get_year_to_date(self, _=None) -> Dict[str, str]:
        """Returns the date range from start of current year to today"""
        today = datetime.now()
        start_date = datetime(today.year, 1, 1)
        return self._format_date_range(start_date, today)

    def _get_quarter_to_date(self, _=None) -> Dict[str, str]:
        """Returns the date range from start of current quarter to today"""
        today = datetime.now()
        current_quarter = (today.month - 1) // 3
        start_date = datetime(today.year, current_quarter * 3 + 1, 1)
        return self._format_date_range(start_date, today)

    def _get_month_to_date(self, _=None) -> Dict[str, str]:
        """Returns the date range from start of current month to today"""
        today = datetime.now()
        start_date = datetime(today.year, today.month, 1)
        return self._format_date_range(start_date, today)

    def get_date_range(self, query: str = None) -> Dict[str, str]:
        """Main interface method to get date range"""
        if not query:
            return self.default_range
            
        query = query.lower().strip()
        
        # Check cache first
        if query in self.cache:
            return self.cache[query]
            
        # Try to extract date range from query
        date_range = self._parse_date_range(query)
        
        # Validate the date range
        if self._validate_date_range(date_range):
            self.cache[query] = date_range
            return date_range
            
        return self.default_range