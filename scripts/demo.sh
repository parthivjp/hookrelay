#!/usr/bin/env bash
# Run after: docker compose up --build
# Sends 5 events to a receiver that fails half the time, so you can see retries working.
API=http://localhost:8000
KEY="X-API-Key: dev-key"

echo "1) Register an endpoint (the demo receiver, secret = whsec_demo)"
curl -s -X POST $API/endpoints -H "$KEY" -H "Content-Type: application/json" \
  -d '{"url":"http://demo-receiver:9000/hook","event_types":["order.*"],"secret":"whsec_demo","description":"demo"}'
echo -e "\n"

echo "2) Send 5 events"
for i in 1 2 3 4 5; do
  curl -s -X POST $API/events -H "$KEY" -H "Content-Type: application/json" \
    -H "Idempotency-Key: demo-order-$i" \
    -d "{\"event_type\":\"order.created\",\"payload\":{\"order_id\":$i,\"amount\":$((i*100))}}" > /dev/null
done
echo "sent."
echo

echo "3) Send order 1 again with the same Idempotency-Key (should say duplicate: true)"
curl -s -X POST $API/events -H "$KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: demo-order-1" \
  -d '{"event_type":"order.created","payload":{"order_id":1,"amount":100}}'
echo -e "\n"

echo "4) Wait a bit, then look at the stats. Run this again after 1-2 minutes."
sleep 15
curl -s $API/stats -H "$KEY"
echo -e "\n"
echo "Dead-letter list:   curl -s '$API/deliveries?status=dead' -H '$KEY'"
echo "Replay a delivery:  curl -s -X POST $API/deliveries/<id>/replay -H '$KEY'"
echo "What receiver got:  curl -s http://localhost:9000/received"
