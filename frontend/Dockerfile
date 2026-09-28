# VeyaCare 前端镜像
#
# 构建产物是纯静态文件，所以最终镜像里只留 nginx + dist，
# node_modules 和源码都不进运行镜像（几百 MB 的差别）。

FROM node:22-alpine AS build

WORKDIR /app

# 先只拷清单，依赖层能被缓存住
COPY package.json package-lock.json ./
# 用 npm ci 而不是 install：按 lock 文件装，构建可复现
RUN npm ci

COPY . .

# 生产态前端走相对路径 /api/**，由下面的 nginx 反代到后端。
# 不注入 VITE_API_BASE，否则会把后端地址烘进 JS，换环境就得重新构建。
RUN npm run build


FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1/ >/dev/null 2>&1 || exit 1

CMD ["nginx", "-g", "daemon off;"]
