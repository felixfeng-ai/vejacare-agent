/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端基址；留空则走相对路径（dev 由 vite 代理转发） */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
