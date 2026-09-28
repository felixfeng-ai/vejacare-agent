/**
 * PM2 进程定义 —— VeyaCare 后端（FastAPI + uvicorn）
 *
 * 用法：pm2 startOrReload ecosystem.config.cjs
 *
 * 为什么用 PM2 而不是 systemd：这台香港服务器上已有的 i面试 / 成剧 两个站点
 * 都是 PM2 托管的，保持同一套运维手法，排查时不用在两套工具之间切换。
 */
module.exports = {
  apps: [
    {
      name: 'veyacare-api',
      cwd: '/opt/veyacare/backend',

      // 直接跑 venv 里的 uvicorn 可执行文件，所以 interpreter 必须是 none，
      // 否则 PM2 会把它当 JS 文件丢给 node 执行。
      script: '.venv/bin/uvicorn',
      args: 'app.main:app --host 127.0.0.1 --port 3002 --proxy-headers',
      interpreter: 'none',

      instances: 1,
      exec_mode: 'fork',
      autorestart: true,

      // 这台机器总共只有 1.9G 内存，还跑着另外两个站点和 postgres。
      // 给一个上限，真泄漏了就重启而不是把整机拖死。
      max_memory_restart: '600M',

      env: {
        // 不加这个，uvicorn 的输出会被 Python 缓冲住，pm2 logs 看不到实时日志
        PYTHONUNBUFFERED: '1',
        PYTHONIOENCODING: 'utf-8',
      },

      out_file: '/opt/veyacare/logs/api.out.log',
      error_file: '/opt/veyacare/logs/api.err.log',
      merge_logs: true,
      time: true,
    },
  ],
};
