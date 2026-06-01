# eCourts Tracker VPS Deploy

FastAPI + PostgreSQL case tracker dashboard for VPS hosting. The app uses `DATABASE_URL` for Postgres and `SOURCE_API_BASE_URL` for your existing case-data API.

## Quick deploy on port 8080

```bash
git clone https://github.com/vigarepo2/eCourtsAPI.git
cd eCourtsAPI
cp .env.example .env
nano .env
docker compose up -d --build
docker compose logs -f app
```

Open `http://YOUR_SERVER_IP:8080`.

## .env

```env
DATABASE_URL=postgresql://ecourts:ecourts@localhost:5432/ecourts
SOURCE_API_BASE_URL=https://your-api.example.com
SOURCE_CASE_PATH=/case/{cino}
SOURCE_ORDERS_PATH=/case/{cino}
BASIC_AUTH_USERNAME=admin
BASIC_AUTH_PASSWORD=change-this-long-password
ORDER_WARN_SECONDS=480
```

If your source endpoint is `https://api.example.com/api/cnr/PBFZC20014232025`, use:

```env
SOURCE_API_BASE_URL=https://api.example.com
SOURCE_CASE_PATH=/api/cnr/{cino}
SOURCE_ORDERS_PATH=/api/cnr/{cino}
```

## Normal VPS run

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip postgresql postgresql-contrib nginx git
git clone https://github.com/vigarepo2/eCourtsAPI.git
cd eCourtsAPI
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
export $(grep -v '^#' .env | xargs)
gunicorn app:app -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8080 --workers 2 --timeout 120
```

## API examples

```bash
curl http://127.0.0.1:8080/api/health
curl -X POST http://127.0.0.1:8080/api/cases -H 'content-type: application/json' -d '{"cino":"PBFZC20014232025","custom_title":"Punjab Gramin Bank vs Bobby Singh"}'
curl 'http://127.0.0.1:8080/api/search?q=bobby'
curl -X POST http://127.0.0.1:8080/api/cases/1/refresh
curl -X POST http://127.0.0.1:8080/api/cases/1/refresh-orders
curl http://127.0.0.1:8080/api/export.json -o backup.json
curl http://127.0.0.1:8080/api/export.csv -o cases.csv
```

With Basic Auth enabled, add `-u admin:your-password` to curl commands.

## PostgreSQL backup

```bash
docker compose exec db pg_dump -U ecourts ecourts > ecourts_$(date +%F).sql
cat ecourts_2026-06-01.sql | docker compose exec -T db psql -U ecourts ecourts
```

## Source JSON

The normalizer supports eCourts-like fields such as `historyOfCaseHearing`, `interimOrder`, `finalOrder`, `transfer`, `processes`, `fir_details`, `pet_name`, `res_name`, `str_error1`, `act`, `court_name`, `desgname`, `purpose_name`, `date_next_list`, `date_last_list`, and `date_of_decision`.
