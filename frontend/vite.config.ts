import { defineConfig, type ProxyOptions } from 'vite';
import react from '@vitejs/plugin-react';

// 只代理到本机 Agent；开发服务器承担同源转发，不放开后端的跨域策略。
const port = Number(process.env.WORKSTATION_AGENT_HTTP_PORT ?? 8766);
if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Agent HTTP 端口无效');
const target = `http://127.0.0.1:${port}`;
const proxy: Record<string, ProxyOptions> = {
  '/api': {
    target,
    changeOrigin: true,
    rewrite: (path) => path.replace(/^\/api/, ''),
    configure(server) {
      server.on('proxyReq', (request) => {
        request.setHeader('Origin', target);
      });
    },
  },
};

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, strictPort: true, proxy },
  preview: { port: 4173, strictPort: true, proxy },
});
