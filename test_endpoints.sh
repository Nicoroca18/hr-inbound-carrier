#!/bin/zsh

# ----------------------------
# Test script for HappyRobot FDE POC
# ----------------------------

# 1️⃣ Authenticate carrier (mock)
echo "1️⃣ Authenticate carrier"
curl -X POST "http://127.0.0.1:8000/api/authenticate" \
  -H "Content-Type: application/json" \
  -H "x-api-key: test-api-key" \
  -d '{"mc_number":"123456"}'
echo "\n"

# 2️⃣ Get available loads
echo "2️⃣ Get available loads"
curl -X GET "http://127.0.0.1:8000/api/loads" \
  -H "x-api-key: test-api-key"
echo "\n"

# 3️⃣ Negotiate load (example offer)
echo "3️⃣ Negotiate load"
curl -X POST "http://127.0.0.1:8000/api/negotiate" \
  -H "Content-Type: application/json" \
  -H "x-api-key: test-api-key" \
  -d '{"mc_number":"123456","load_id":"L001","offer":1200}'
echo "\n"

# 4️⃣ Post call result (extract entities & sentiment)
echo "4️⃣ Post call result"
curl -X POST "http://127.0.0.1:8000/api/call/result" \
  -H "Content-Type: application/json" \
  -H "x-api-key: test-api-key" \
  -d '{
        "transcript":"My MC number is MC 123456. I can do L001 for $1350, sounds good.",
        "mc_number":"123456",
        "load_id":"L001",
        "final_price":1350,
        "accepted":true
      }'
echo "\n"

echo "✅ All endpoints tested!"

