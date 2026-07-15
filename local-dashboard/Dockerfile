FROM node:22-alpine

WORKDIR /app
COPY package.json ./
COPY server.mjs ./
COPY public ./public

ENV DASHBOARD_HOST=0.0.0.0
ENV DASHBOARD_PORT=4173
EXPOSE 4173

USER node
CMD ["node", "server.mjs"]
