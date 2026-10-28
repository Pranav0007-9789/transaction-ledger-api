# API Documentation

Interactive OpenAPI/Swagger documentation is available at `http://127.0.0.1:8000/docs` while the server is running.

## 1. Health check
- **GET** `/`
- Success: `200 OK`

## 2. Create account
- **POST** `/accounts`
- Request: `{"name":"Pranav"}`
- Success: `201 Created`
- Returns: `id`, `name`, `balance`, `created_at`
- Validation: name is required; initial balance is server-controlled and always zero.

## 3. Create transaction
- **POST** `/accounts/{account_id}/transactions`
- Request:
  ```json
  {
    "type": "credit",
    "amount": 100,
    "description": "Deposit",
    "reference_id": "ref-001"
  }
  ```
- Success: `201 Created`
- Errors: `400` insufficient balance; `404` account not found; `409` duplicate reference; `422` invalid input.

## 4. Get balance
- **GET** `/accounts/{account_id}/balance`
- Success: `200 OK`
- Response: `{"account_id":"...","balance":100.0}`
- Error: `404` account not found.

## 5. Get transaction history
- **GET** `/accounts/{account_id}/transactions`
- Optional query parameters: `type=credit|debit`, `page=1`, `limit=10`
- Success: `200 OK`
- Sorted newest first.
- Response includes `pagination.current_page`, `page_size`, `total_records`, `total_pages`, and `transactions`.
- Errors: `404` account not found; `422` invalid type/page/limit.

## 6. Transfer (bonus)
- **POST** `/transfers`
- Request:
  ```json
  {
    "source_account_id": "source-uuid",
    "destination_account_id": "destination-uuid",
    "amount": 50,
    "description": "Transfer",
    "reference_id": "transfer-001"
  }
  ```
- Success: `201 Created`
- Errors: `400` insufficient balance; `404` account not found; `409` duplicate reference; `422` invalid amount or same account.
- Source debit, destination credit, and both balance updates are committed atomically.
