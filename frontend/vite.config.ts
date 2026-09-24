import react from "@vitejs/plugin-react";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

// Backend origin the dev-server proxy forwards REST calls to. Overridable
// via BACKEND_ORIGIN in the shell env (not VITE_-prefixed, since it's a
// build-tool-only setting, not something app code reads) -- handy since the
// PRD's default port 8000 may already be taken by something else on your
// machine, in which case run the backend on another port and set this.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backendOrigin = env.BACKEND_ORIGIN || "http://localhost:8000";

  return {
    plugins: [react()],
    server: {
      host: true,
      port: 5173,
      proxy: {
        // Only the REST paths this app calls -- avoids needing CORS
        // middleware on the backend for local dev. The live seat stream
        // (WebSocket) is NOT proxied here; see src/lib/seatStreamTransport.ts
        // for how that's configured (mock by default, real URL via
        // VITE_WS_URL once the WS server exists).
        "/sessions": backendOrigin,
        "/events": backendOrigin,
      },
    },
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      globals: true,
    },
  };
});
