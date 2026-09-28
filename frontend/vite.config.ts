import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * 后端基址有两种接法：
 * 1) 建 .env 并写 VITE_API_BASE=http://127.0.0.1:8000 —— 浏览器直连后端（后端需开 CORS）。
 * 2) 不建 .env —— 前端走相对路径 /api/**，由下面的 dev 代理转发到后端，天然没有 CORS 问题。
 *    生产环境则要求后端与前端同源部署（或由 Nginx 把 /api 反代到后端）。
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
});
