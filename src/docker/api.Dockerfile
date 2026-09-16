# syntax=docker/dockerfile:1
# Build the Java PrivateAPI WAR from the HRBC snapshot and locally built shared modules.
ARG RUNTIME_IMAGE
FROM 710983083415.dkr.ecr.ap-northeast-1.amazonaws.com/base/tomcat:AZ2023-J11.0.29-T7.0.76 AS build
# Select an installed JDK explicitly so Gradle has compiler tools, not just a JRE.
RUN dnf install -y git java-11-amazon-corretto-devel && dnf clean all
RUN set -eu; javac_path=$(find /usr/lib/jvm -type f -name javac | head -n 1); test -n "$javac_path"; ln -s "$(dirname "$(dirname "$javac_path")")" /opt/feature-java
ENV GRADLE_OPTS=-Dfile.encoding=UTF-8
ENV JAVA_TOOL_OPTIONS=-Dfile.encoding=UTF-8
ENV JAVA_HOME=/opt/feature-java
ENV PATH=/opt/feature-java/bin:${PATH}

COPY api/ /src/
WORKDIR /src/api_source
# Publish shared modules in dependency order before assembling the consuming WAR.
RUN --mount=type=cache,target=/root/.gradle \
    set -eu; for project in UtilGeneral Core CoreLegacy HrbcDb HrbcGeneral; do \
      (cd "$project" && bash ./gradlew --no-daemon --console=plain assemble publishMainPublicationToMavenLocal); \
    done; \
    cd PrivateAPI; bash ./gradlew --no-daemon --console=plain assemble; \
    mkdir /out; cp build/libs/*.war /out/PrivateAPI.war
# Remove the old exploded app so Tomcat deploys the replacement WAR on startup.
FROM ${RUNTIME_IMAGE}
RUN rm -rf /usr/share/tomcat7/webapps/PrivateAPI
COPY --from=build /out/PrivateAPI.war /usr/share/tomcat7/webapps/PrivateAPI.war
# Check the application's no-auth Memcached status endpoint after startup.
HEALTHCHECK --interval=10s --timeout=5s --start-period=60s --retries=24 CMD curl -fsS http://127.0.0.1:8080/PrivateAPI/privateapi/noauth/status/memcache >/dev/null || exit 1