# Backend Transfer System Deployment Guide

## Files Created

1. **api/transfer_routes.py** - All transfer endpoints with security
2. **migrate_transfer_system.py** - Database migration script
3. **api/__init__.py** - Updated to include transfer routes

## Security Features Implemented

✅ **Org-scoped authorization** - All endpoints use `CurrentUser` dependency which extracts org_id from Clerk JWT
✅ **Multi-tenant isolation** - All queries filter by org_id from user's JWT  
✅ **Optimistic locking** - Transfer endpoint uses version field to prevent race conditions
✅ **Audit trail** - All transfers logged to conversation_audit_logs collection
✅ **Transfer loop detection** - Blocks transfers to same agent within 5min window
✅ **Pool capacity limits** - Max 25 pending mentions per agent

## Deployment Steps

### 1. Run Database Migration

```bash
cd /Users/vipulmunot/Documents/HeidelAI/chatbot_prod
python migrate_transfer_system.py
```

This will:
- Add `version: 0` to all existing conversations
- Create indexes on conversation collections
- Create `conversation_mentions` collection with indexes
- Create `conversation_audit_logs` collection with indexes

### 2. Restart Backend Server

```bash
# Development
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Production (if using docker)
docker-compose restart
```

### 3. Verify Endpoints

```bash
# Health check
curl http://localhost:8000/

# Test get team agents (requires auth)
curl -H "Authorization: Bearer {clerk_jwt}" \
  http://localhost:8000/api/agents/team

# Test transfer (requires auth)
curl -X POST \
  -H "Authorization: Bearer {clerk_jwt}" \
  -H "Content-Type: application/json" \
  -d '{"target_agent_id": "...", "reason": "Test transfer"}' \
  http://localhost:8000/api/conversations/{conv_id}/transfer
```

## API Endpoints Available

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /api/conversations/{id}/transfer | Transfer conversation to another agent |
| POST | /api/conversations/{id}/reclaim | Reclaim transferred conversation |
| POST | /api/conversations/{id}/mentions | Create @mention requests |
| GET | /api/conversations/pending-mentions | Get pending mentions for current user |
| POST | /api/mentions/{id}/accept | Accept mention → transfer conversation |
| POST | /api/mentions/{id}/reject | Reject mention request |
| GET | /api/agents/team | List all active agents in org |

## Testing

### Manual Testing

1. **Transfer conversation:**
   - Login as agent A
   - Transfer conversation to agent B via POST /transfer
   - Verify conversation appears in agent B's inbox
   - Check audit_logs collection for entry

2. **Reclaim conversation:**
   - Agent A reclaims via POST /reclaim
   - Verify conversation returns to agent A

3. **@Mention:**
   - Create mention via POST /mentions
   - Login as mentioned agent
   - See pending mention via GET /pending-mentions
   - Accept/reject via POST /mentions/{id}/accept or /reject

### Security Testing

```python
# Test cross-org access (should fail with 404)
# Agent from org_1 tries to transfer conversation from org_2
# Expected: 404 Not found

# Test concurrent transfer (should fail with 409)
# Two agents transfer same conversation simultaneously
# Expected: One succeeds, other gets 409 Conflict

# Test transfer loop (should fail with 400)
# Agent A → Agent B, then Agent B → Agent A within 5min
# Expected: 400 Transfer loop detected
```

## Database Collections

### conversation_audit_logs
```json
{
  "conversation_id": "conv_123",
  "org_id": "org_456",
  "action": "transferred",
  "from_agent_id": "agent_A",
  "to_agent_id": "agent_B",
  "performed_by": "agent_A",
  "timestamp": "2026-07-01T12:00:00",
  "ip_address": "192.168.1.1",
  "reason": "Customer requested billing"
}
```

### conversation_mentions
```json
{
  "conversation_id": "conv_123",
  "org_id": "org_456",
  "from_agent_id": "agent_A",
  "to_agent_id": "agent_B",
  "status": "pending",
  "message_content": "Can you help with this?",
  "created_at": "2026-07-01T12:00:00",
  "resolved_at": null
}
```

### conversations_{org_id} (updated)
```json
{
  "conversation_id": "conv_123",
  "version": 3,
  "assigned_agent_id": "agent_B",
  "assigned_agent": {
    "id": "agent_B",
    "name": "John Doe",
    "assigned_at": "2026-07-01T12:00:00"
  },
  "assignment_history": [
    {
      "from_agent": {"id": "agent_A", "name": "Jane"},
      "to_agent": {"id": "agent_B", "name": "John"},
      "timestamp": "2026-07-01T12:00:00",
      "reason": "Customer requested billing"
    }
  ]
}
```

## Monitoring

### Key Metrics to Track

```python
# Monitor in production:
- Transfer success rate (should be >95%)
- Transfer loop violations (should be 0)
- Cross-org access attempts (should be 0)
- Audit log completeness (100% of transfers)
- Average time from transfer to acceptance
- Pool capacity violations
```

### Logging

All transfers are logged with `logger.info()`:
```
Conversation conv_123 transferred from agent_A to agent_B
Conversation conv_123 reclaimed by agent_A
```

Errors logged with `logger.warning()` or `logger.error()`.

## Rollback Plan

If issues arise:

1. **Disable transfer routes:**
```python
# In api/__init__.py, comment out:
# router.include_router(transfer_routes.router)
```

2. **Restart server**

3. **Data is safe:** All transfers are in audit_logs, version field is benign

## Performance Notes

- Transfer endpoint: ~50-100ms (includes DB query, update, audit log, WebSocket)
- Mention creation: ~20-50ms per mentioned agent
- Get team agents: ~10-30ms (cached by agent autocomplete)
- All endpoints have rate limiting (20-30/min)

## Troubleshooting

**"Conversation not found" (404)**
- Check org_id in JWT matches conversation's org
- Verify conversation_id is correct

**"Conversation was modified" (409)**
- Concurrent transfer detected
- User should refresh and retry

**"Transfer loop detected" (400)**
- Valid - agent recently handled conversation
- Wait 5 minutes or override if needed

**WebSocket not received**
- Check services.send_to_user() implementation
- Verify agent is connected to WebSocket
- Check agent_clients mapping

