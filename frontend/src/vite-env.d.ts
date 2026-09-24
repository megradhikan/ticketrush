/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_EVENT_ID?: string;
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_WS_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
