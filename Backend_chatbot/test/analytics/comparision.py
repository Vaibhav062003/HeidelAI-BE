from datetime import datetime, timedelta
from typing import Dict, Any, List
from dateutil.relativedelta import relativedelta

class ComparisonAnalytics:
    def __init__(self):
        self.comparison_types = {
            "mom": self._month_over_month,
            "yoy": self._year_over_year,
            "wow": self._week_over_week,
            "custom": self._custom_comparison
        }

    async def compare_periods(
        self, 
        query: str, 
        current_range: Dict[str, str],
        analytics_handler
    ) -> Dict[str, Any]:
        """Compare analytics data between different time periods"""
        
        comparison_type = self._detect_comparison_type(query)
        compare_func = self.comparison_types.get(comparison_type, self._custom_comparison)
        
        # Get comparison date ranges
        current_period, previous_period = compare_func(current_range)
        
        # Fetch data for both periods
        current_data = await analytics_handler.process_analytics_query(
            query, 
            current_period["start_date"],
            current_period["end_date"]
        )
        
        previous_data = await analytics_handler.process_analytics_query(
            query,
            previous_period["start_date"],
            previous_period["end_date"]
        )
        
        # Calculate metrics comparison
        comparison = self._calculate_comparison(current_data, previous_data)
        
        return {
            "current_period": current_data,
            "previous_period": previous_data,
            "comparison": comparison,
            "type": comparison_type
        }

    def _detect_comparison_type(self, query: str) -> str:
        """Detect the type of comparison requested"""
        query = query.lower()
        if "month over month" in query or "mom" in query:
            return "mom"
        elif "year over year" in query or "yoy" in query:
            return "yoy"
        elif "week over week" in query or "wow" in query:
            return "wow"
        return "custom"

    def _calculate_comparison(
        self, 
        current_data: Dict[str, Any], 
        previous_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Calculate percentage changes and differences between periods"""
        comparison = {}
        
        try:
            if "google_analytics" in current_data:
                comparison["metrics"] = self._compare_ga_metrics(
                    current_data.get("google_analytics", {}),
                    previous_data.get("google_analytics", {})
                )
            
            if "instagram" in current_data:
                comparison["social"] = self._compare_social_metrics(
                    current_data.get("instagram", {}),
                    previous_data.get("instagram", {})
                )
        except Exception as e:
            comparison["error"] = str(e)
            
        return comparison

    def _compare_ga_metrics(self, current: Dict, previous: Dict) -> Dict:
        """Compare Google Analytics metrics"""
        comparison = {}
        for metric in current:
            if metric in previous:
                current_val = float(current[metric])
                previous_val = float(previous[metric])
                
                if previous_val != 0:
                    change_pct = ((current_val - previous_val) / previous_val) * 100
                else:
                    change_pct = 100 if current_val > 0 else 0
                    
                comparison[metric] = {
                    "current": current_val,
                    "previous": previous_val,
                    "change_percentage": round(change_pct, 2),
                    "absolute_change": round(current_val - previous_val, 2)
                }
                
        return comparison

    # Time period calculation methods
    def _month_over_month(self, current_range: Dict[str, str]) -> tuple:
        current_start = datetime.strptime(current_range["start_date"], "%Y-%m-%d")
        current_end = datetime.strptime(current_range["end_date"], "%Y-%m-%d")
        
        previous_end = current_start - timedelta(days=1)
        previous_start = previous_end - relativedelta(months=1) + timedelta(days=1)
        
        return (
            current_range,
            {
                "start_date": previous_start.strftime("%Y-%m-%d"),
                "end_date": previous_end.strftime("%Y-%m-%d")
            }
        )
    
    def _year_over_year(self, current_range: Dict[str, str]) -> tuple:
        """Calculate year over year comparison dates"""
        current_start = datetime.strptime(current_range["start_date"], "%Y-%m-%d")
        current_end = datetime.strptime(current_range["end_date"], "%Y-%m-%d")
        
        # Calculate the same date range from previous year
        previous_start = current_start - relativedelta(years=1)
        previous_end = current_end - relativedelta(years=1)
        
        return (
            current_range,
            {
                "start_date": previous_start.strftime("%Y-%m-%d"),
                "end_date": previous_end.strftime("%Y-%m-%d")
            }
        )

    def _week_over_week(self, current_range: Dict[str, str]) -> tuple:
        """Calculate week over week comparison dates"""
        current_start = datetime.strptime(current_range["start_date"], "%Y-%m-%d")
        current_end = datetime.strptime(current_range["end_date"], "%Y-%m-%d")
        
        # Calculate previous week's date range
        previous_end = current_start - timedelta(days=1)
        previous_start = previous_end - timedelta(days=6)  # 7 days total
        
        return (
            current_range,
            {
                "start_date": previous_start.strftime("%Y-%m-%d"),
                "end_date": previous_end.strftime("%Y-%m-%d")
            }
        )

    def _custom_comparison(self, current_range: Dict[str, str]) -> tuple:
        """Handle custom date range comparisons"""
        current_start = datetime.strptime(current_range["start_date"], "%Y-%m-%d")
        current_end = datetime.strptime(current_range["end_date"], "%Y-%m-%d")
        
        # Calculate the previous period with same duration
        date_diff = current_end - current_start
        previous_end = current_start - timedelta(days=1)
        previous_start = previous_end - date_diff
        
        return (
            current_range,
            {
                "start_date": previous_start.strftime("%Y-%m-%d"),
                "end_date": previous_end.strftime("%Y-%m-%d")
            }
        )

    def _get_period_duration(self, start_date: str, end_date: str) -> int:
        """Calculate the duration between two dates in days"""
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        return (end - start).days

    def _validate_date_range(self, start_date: str, end_date: str) -> bool:
        """Validate that the date range is valid"""
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d")
            return start <= end
        except ValueError:
            return False

    def _format_comparison_result(
        self,
        current_period: Dict[str, Any],
        previous_period: Dict[str, Any],
        metrics_comparison: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Format the comparison results in a structured way"""
        return {
            "current_period": {
                "date_range": f"{current_period['start_date']} to {current_period['end_date']}",
                "metrics": current_period.get("metrics", {})
            },
            "previous_period": {
                "date_range": f"{previous_period['start_date']} to {previous_period['end_date']}",
                "metrics": previous_period.get("metrics", {})
            },
            "comparison": {
                "metrics": metrics_comparison,
                "summary": self._generate_comparison_summary(metrics_comparison)
            }
        }

    def _generate_comparison_summary(self, metrics_comparison: Dict[str, Any]) -> str:
        """Generate a natural language summary of the comparison"""
        summary_points = []
        
        for metric, data in metrics_comparison.items():
            if isinstance(data, dict) and "change_percentage" in data:
                change = data["change_percentage"]
                if change > 0:
                    direction = "increased"
                elif change < 0:
                    direction = "decreased"
                else:
                    direction = "remained the same"
                    
                summary_points.append(
                    f"{metric} {direction} by {abs(change):.1f}%"
                )
        
        return " | ".join(summary_points)