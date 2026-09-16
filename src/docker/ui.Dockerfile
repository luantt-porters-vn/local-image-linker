# syntax=docker/dockerfile:1
# Compile the local client and UI snapshots, then serve the bundle in the saved runtime.
# COPY paths refer to the generated snapshot context, not this Dockerfile's directory.
ARG RUNTIME_IMAGE
FROM 710983083415.dkr.ecr.ap-northeast-1.amazonaws.com/base/node:AZ2023-N16.20.2 AS build
ENV npm_config_fetch_retries=3 npm_config_fetch_timeout=300000 npm_config_maxsockets=5
ENV npm_config_registry=http://registry.ps.porters.local:8081/repository/npm-group/
# Cache downloads while mounting npm credentials only for dependency installation.
WORKDIR /src/client
COPY client/package*.json ./
RUN --mount=type=secret,id=npmrc,target=/root/.npmrc --mount=type=cache,target=/root/.npm npm ci --ignore-scripts
COPY client/ ./
RUN npm run build && npm pack --ignore-scripts && mv *.tgz /tmp/client.tgz
WORKDIR /src/ui
COPY ui/package*.json ./
RUN --mount=type=secret,id=npmrc,target=/root/.npmrc --mount=type=cache,target=/root/.npm npm ci --ignore-scripts
COPY ui/ ./
# Keep the UI lockfile dependency tree intact when substituting the local library.
# Fail explicitly if the local library introduces dependencies the UI does not supply.
RUN node -e 'const semver=require("semver"); const p=require("/src/client/package.json"); for(const [name,range] of Object.entries({...p.dependencies,...p.peerDependencies})){const v=require(name+"/package.json").version;if(!semver.satisfies(v,range))throw Error(name+" does not satisfy local client dependency "+range);}' \
    && rm -rf node_modules/@hrbc/api-client-private \
    && mkdir -p node_modules/@hrbc/api-client-private \
    && tar -xzf /tmp/client.tgz --strip-components=1 -C node_modules/@hrbc/api-client-private \
    && npm run build
# Keep the existing nginx startup configuration, replacing only the compiled bundle.
FROM ${RUNTIME_IMAGE}
RUN rm -rf /home/nginx/www/build
COPY --from=build /src/ui/build/ /home/nginx/www/build/
# Readiness checks asset delivery, not authenticated application behavior.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=18 CMD curl -fsS -H 'Host: feature.localvm' http://127.0.0.1/tsbundle/asset-manifest.json >/dev/null || exit 1