# Optional reproducible Linux verification; build context is frontend/.
FROM node:24-bookworm-slim
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1 \
    NODE_OPTIONS=--max-old-space-size=2048
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run typecheck
RUN npm run lint
RUN npm test
ARG BACKEND_URL=http://host.docker.internal:8000
ENV BACKEND_URL=$BACKEND_URL
RUN npm run build
EXPOSE 3000
CMD ["npm", "start", "--", "--hostname", "0.0.0.0"]
