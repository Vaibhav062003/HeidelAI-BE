from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from database import get_async_mongo_db
from core.logger import get_logger

logger = get_logger(__name__)

async def aggregate_dashboard_stats(
    org_id: str, 
    target_date_str: Optional[str] = None,
    start_date_str: Optional[str] = None,
    end_date_str: Optional[str] = None
) -> Dict:
    """
    Aggregates metrics for the HeidelAI Dashboard for a specific date or range.
    Calculates comparisons against the previous day if no range is provided.
    """
    try:
        async_db = get_async_mongo_db()
        conv_col = async_db[f"conversations_{org_id}"]
        scheduled_col = async_db["scheduled_messages"]
        
        # Determine target date range
        is_custom_range = bool(start_date_str and end_date_str)
        
        if is_custom_range:
            try:
                start_of_day = datetime.fromisoformat(start_date_str.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
                end_of_day = datetime.fromisoformat(end_date_str.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
            except ValueError:
                # Fallback to defaults if parsing fails
                target_date = datetime.now(timezone.utc)
                start_of_day = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
                end_of_day = start_of_day + timedelta(days=1)
                is_custom_range = False
        else:
            if target_date_str:
                try:
                    target_date = datetime.fromisoformat(target_date_str.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
                except ValueError:
                    target_date = datetime.now(timezone.utc)
            else:
                target_date = datetime.now(timezone.utc)

            start_of_day = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
            end_of_day = start_of_day + timedelta(days=1)
        
        # Only calculate previous day if not a custom range
        if not is_custom_range:
            start_of_prev_day = start_of_day - timedelta(days=1)
            end_of_prev_day = start_of_day
        else:
            start_of_prev_day = None
            end_of_prev_day = None
        
        now = datetime.now(timezone.utc)
        expiring_limit = now + timedelta(hours=2)

        async def get_stats_for_range(start, end):
            pipeline = [
                {
                    "$match": {
                        "started_at": {"$gte": start, "$lt": end}
                    }
                },
                {
                    "$facet": {
                        "highLevel": [
                            {
                                "$group": {
                                    "_id": None,
                                    "total": {"$sum": 1},
                                    "conversions": {"$sum": {"$cond": [{"$eq": ["$status", "closed"]}, 1, 0]}},
                                    "ai_handled": {"$sum": {"$cond": ["$is_ai_enabled", 1, 0]}},
                                    "human_handled": {"$sum": {"$cond": [{"$not": "$is_ai_enabled"}, 1, 0]}},
                                    "hot_leads": {"$sum": {"$cond": [{"$and": [{"$eq": ["$priority", "high"]}, {"$eq": ["$status", "open"]}]}, 1, 0]}}
                                }
                            }
                        ],
                        "statusCounts": [
                            {"$group": {"_id": "$status", "count": {"$sum": 1}}}
                        ],
                        "conversationFlow": [
                            {
                                "$group": {
                                    "_id": {
                                        "is_ai": "$is_ai_enabled",
                                        "status": "$status"
                                    },
                                    "count": {"$sum": 1}
                                }
                            }
                        ],
                        "leadFunnel": [
                            {"$group": {"_id": "$priority", "count": {"$sum": 1}}}
                        ],
                        "hotLeadDetails": [
                            {
                                "$match": {
                                    "status": "open",
                                    "priority": "high"
                                }
                            },
                            {
                                "$project": {
                                    "_id": 0,
                                    "conversation_id": 1,
                                    "customer_name": 1,
                                    "customer_id": 1,
                                    "platform": 1,
                                    "customer_username": 1,
                                    "last_message": 1,
                                    "last_message_timestamp": 1
                                }
                            },
                            {"$limit": 10}
                        ]
                    }
                }
            ]
            results = await conv_col.aggregate(pipeline).to_list(length=1)
            return results[0] if results else {}

        # Fetch stats for current and previous period
        current_data = await get_stats_for_range(start_of_day, end_of_day)
        
        if is_custom_range:
            prev_data = {}
        else:
            prev_data = await get_stats_for_range(start_of_prev_day, end_of_prev_day)

        # Helper to calculate change
        def calc_change(curr, prev):
            if prev == 0:
                return 100.0 if curr > 0 else 0.0
            return round(((curr - prev) / prev) * 100, 2)

        # 1. At a Glance
        curr_hl = current_data.get("highLevel", [{}])[0] if current_data.get("highLevel") else {}
        prev_hl = prev_data.get("highLevel", [{}])[0] if prev_data.get("highLevel") else {}
        
        total_curr = curr_hl.get("total", 0)
        conv_curr = curr_hl.get("conversions", 0)
        rate_curr = round((conv_curr / total_curr * 100), 1) if total_curr > 0 else 0
        
        total_prev = prev_hl.get("total", 0)
        conv_prev = prev_hl.get("conversions", 0)
        rate_prev = round((conv_prev / total_prev * 100), 1) if total_prev > 0 else 0

        stats = {
            "conversionRate": {
                "value": rate_curr,
                "change": calc_change(rate_curr, rate_prev)
            },
            "totalConversations": {
                "value": total_curr,
                "change": calc_change(total_curr, total_prev)
            },
            "aiVsHuman": {
                "ai": curr_hl.get("ai_handled", 0),
                "human": curr_hl.get("human_handled", 0),
                "change": calc_change(curr_hl.get("ai_handled", 0) + curr_hl.get("human_handled", 0), total_prev) # General volume change
            },
            "hotLeads": {
                "value": curr_hl.get("hot_leads", 0),
                "change": calc_change(curr_hl.get("hot_leads", 0), prev_hl.get("hot_leads", 0))
            }
        }

        # 2. Lead Patrol - Status Counters (Current Day)
        status_map = {item["_id"]: item["count"] for item in current_data.get("statusCounts", [])}
        
        # Expiring WhatsApp (Always relative to NOW)
        expiring_res = await conv_col.aggregate([
            {
                "$match": {
                    "status": "open",
                    "reply_window_ends_at": {"$gte": now, "$lte": expiring_limit}
                }
            },
            {"$count": "count"}
        ]).to_list(length=1)
        
        lead_patrol_stats = {
            "open": status_map.get("open", 0),
            "closed": status_map.get("closed", 0),
            "follow-up": status_map.get("follow-up", 0),
            "waiting": status_map.get("waiting", 0),
            "overdue": 0,
            "expiringWhatsApp": expiring_res[0].get("count", 0) if expiring_res else 0,
            "expiringWhatsAppDetails": await conv_col.find(
                {
                    "status": "open",
                    "reply_window_ends_at": {"$gte": now, "$lte": expiring_limit}
                },
                {
                    "_id": 0,
                    "conversation_id": 1,
                    "customer_name": 1,
                    "customer_id": 1,
                    "platform": 1,
                    "last_message": 1,
                    "reply_window_ends_at": 1
                }
            ).to_list(length=10)
        }

        # 3. Lead Funnel (Current Day)
        priority_map = {item["_id"]: item["count"] for item in current_data.get("leadFunnel", [])}
        engaged_count = priority_map.get("low", 0) + priority_map.get("medium", 0) + priority_map.get("high", 0)
        
        funnel = [
            {"label": "New Leads", "value": total_curr, "total": total_curr, "change": 100, "color": "#10b981"},
            {"label": "Engaged Leads", "value": engaged_count, "total": total_curr, "change": round((engaged_count/total_curr*100), 1) if total_curr > 0 else 0, "color": "#3b82f6"},
            {"label": "High Intent", "value": priority_map.get("high", 0), "total": total_curr, "change": round((priority_map.get('high', 0)/total_curr*100), 1) if total_curr > 0 else 0, "color": "#f59e0b"},
            {"label": "Converted", "value": conv_curr, "total": total_curr, "change": round((conv_curr/total_curr*100), 1) if total_curr > 0 else 0, "color": "#8b5cf6"}
        ]
        
        # Bottom boxes in screenshot
        overall_metrics = {
            "overallConversion": rate_curr,
            "engagementRate": round((engaged_count / total_curr * 100), 1) if total_curr > 0 else 0
        }

        # 4. Engagement Control Center - Conversation Flow (Current Day)
        flow_data = current_data.get("conversationFlow", [])
        flow = [
            {"name": "AI - Human Transfers", "value": 0}, 
            {"name": "Fully AI - converted", "value": sum(i["count"] for i in flow_data if i["_id"].get("is_ai") and i["_id"].get("status") == "closed")},
            {"name": "Human converted", "value": sum(i["count"] for i in flow_data if not i["_id"].get("is_ai") and i["_id"].get("status") == "closed")},
            {"name": "Dropped / Ghosted", "value": 0},
            {"name": "Window Shoppers", "value": sum(i["count"] for i in flow_data if i["_id"].get("is_ai") and i["_id"].get("status") == "open")}
        ]

        # 5. Performance Tracking (Current Day / Range)
        human_performance = []
        human_res = await conv_col.aggregate([
            {"$match": {"started_at": {"$gte": start_of_day, "$lt": end_of_day}, "assigned_agent": {"$ne": None}}},
            {"$group": {"_id": "$assigned_agent.username", "count": {"$sum": 1}}}
        ]).to_list(length=10)
        
        for item in human_res:
            human_performance.append({
                "agent": item["_id"],
                "rating": 4.8,
                "conversations": item["count"],
                "aiAssist": 85
            })

        # 6. Follow-up Tracker (Current Status)
        today_start_now = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end_now = today_start_now + timedelta(days=1)
        next_7_days_end = today_start_now + timedelta(days=7)

        followup_pipeline = [
            {"$match": {"org_id": org_id}},
            {
                "$facet": {
                    "overview": [
                        {
                            "$group": {
                                "_id": None,
                                "dueToday": {"$sum": {"$cond": [{"$and": [{"$eq": ["$status", "pending"]}, {"$gte": ["$scheduled_time", today_start_now]}, {"$lt": ["$scheduled_time", today_end_now]}]}, 1, 0]}},
                                "upcoming7Days": {"$sum": {"$cond": [{"$and": [{"$eq": ["$status", "pending"]}, {"$gte": ["$scheduled_time", today_end_now]}, {"$lt": ["$scheduled_time", next_7_days_end]}]}, 1, 0]}},
                                "missed": {"$sum": {"$cond": [{"$and": [{"$eq": ["$status", "pending"]}, {"$lt": ["$scheduled_time", now]}]}, 1, 0]}}
                            }
                        }
                    ],
                    "upcomingList": [
                        {"$match": {"status": "pending", "scheduled_time": {"$gte": now}}},
                        {"$sort": {"scheduled_time": 1}},
                        {"$limit": 5}
                    ]
                }
            }
        ]
        
        followup_results = await scheduled_col.aggregate(followup_pipeline).to_list(length=1)
        followup_tracker = {"dueToday": 0, "upcoming7Days": 0, "missed": 0, "upcomingList": []}
        
        if followup_results:
            fr = followup_results[0]
            ov = fr.get("overview", [{}])[0] if fr.get("overview") else {}
            followup_tracker.update({
                "dueToday": ov.get("dueToday", 0),
                "upcoming7Days": ov.get("upcoming7Days", 0),
                "missed": ov.get("missed", 0)
            })
            
            for f in fr.get("upcomingList", []):
                conv = await conv_col.find_one({"conversation_id": f["conversation_id"]}, {"customer_name": 1})
                name = conv.get("customer_name") if conv else "Customer"
                diff = f["scheduled_time"] - now
                due_in = f"In {int(diff.total_seconds() // 60)}m" if diff.total_seconds() < 3600 else f"In {int(diff.total_seconds() // 3600)}h"
                
                followup_tracker["upcomingList"].append({
                    "name": name,
                    "product": "Product",
                    "platform": f.get("platform", "whatsapp"),
                    "dueIn": due_in,
                    "conversation_id": f["conversation_id"],
                    "customer_id": f.get("receiver_id")
                })

        # 7. Alerts
        alerts = []
        if stats["hotLeads"]["value"] > 0:
            alerts.append({
                "id": "hot-leads",
                "type": "urgent",
                "title": f"{stats['hotLeads']['value']} High-Intent leads waiting",
                "subtitle": "Immediate action required",
                "time": "Just now",
                "details": current_data.get("hotLeadDetails", [])
            })
        
        if lead_patrol_stats["expiringWhatsApp"] > 0:
            alerts.append({
                "id": "wa-expiring",
                "type": "warning",
                "title": f"{lead_patrol_stats['expiringWhatsApp']} WhatsApp sessions expiring",
                "subtitle": "Reply within the 24h window",
                "time": "Soon",
                "details": lead_patrol_stats["expiringWhatsAppDetails"]
            })

        return {
            "stats": stats,
            "leadPatrol": funnel,
            "overallMetrics": overall_metrics,
            "statusCounters": lead_patrol_stats,
            "conversationFlow": flow,
            "aiMetrics": {
                "conversationsHandled": curr_hl.get("ai_handled", 0),
                "avgResponseTime": 12,
                "escalationRate": 15.4
            },
            "humanPerformance": human_performance,
            "followupTracker": followup_tracker,
            "alerts": alerts
        }

    except Exception as e:
        logger.error(f"Error aggregating dashboard stats for {org_id}: {e}")
        return {}
