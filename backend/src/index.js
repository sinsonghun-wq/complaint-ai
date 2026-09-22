import "dotenv/config";
import express from "express";
import pg from "pg";

const { Pool } = pg;
const port = Number(process.env.PORT || 3001);
const databaseUrl = process.env.DATABASE_URL;

if (!databaseUrl) {
  throw new Error("DATABASE_URL is required. Copy .env.example to .env and set it.");
}

const pool = new Pool({ connectionString: databaseUrl });
const app = express();
app.use(express.json());

app.get("/health", (_req, res) => {
  res.json({ status: "ok", service: "complaintai-backend" });
});

app.get("/health/database", async (_req, res, next) => {
  try {
    const result = await pool.query("SELECT extversion FROM pg_extension WHERE extname = 'vector'");
    res.json({ status: "ok", database: "connected", pgvector: result.rows[0]?.extversion ?? null });
  } catch (error) {
    next(error);
  }
});

app.use((error, _req, res, _next) => {
  console.error(error);
  res.status(500).json({ status: "error", message: "Database connection failed." });
});

app.listen(port, () => {
  console.log(`ComplaintAI backend listening on http://localhost:${port}`);
});

