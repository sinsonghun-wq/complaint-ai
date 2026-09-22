# ComplaintAI backend starter

## Run locally

1. Complete Docker Desktop setup and start Docker Desktop.
2. From the project folder, run `docker compose up -d`.
3. Copy `backend/.env.example` to `backend/.env`.
4. In `backend`, run `npm install` and then `npm run dev`.

Check `http://localhost:3001/health` for the service and
`http://localhost:3001/health/database` for PostgreSQL plus pgvector. The
database is exposed locally on port 5433 to avoid conflicts with other local
PostgreSQL installations.
