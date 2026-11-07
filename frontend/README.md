# Frontend

The Next.js 16 web interface for the Kubernetes Platform. See the [root README](../README.md) for setup and
[documentation/architecture.md](../documentation/architecture.md#frontend-architecture) for design notes.

| Command | Purpose |
|---|---|
| `npm run dev` | Development server on :3000 (proxies `/api/*` to `BACKEND_URL`, default `http://localhost:8000`) |
| `npm run build` / `npm start` | Production build / server |
| `npm run typecheck` | Generate route types and run `tsc` |
| `npm run lint` | ESLint (flat config) |
| `npm test` | Vitest unit tests |

Layout: `app/` holds thin route files, `features/` holds page-level UI, `components/` holds shared UI
(`ui/` is shadcn), and `lib/` holds the API client, navigation model and formatting.
