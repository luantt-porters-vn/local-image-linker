# syntax=docker/dockerfile:1
# Compile legacy assets, then package local PHP and static source into the saved runtime.
ARG RUNTIME_IMAGE
FROM 710983083415.dkr.ecr.ap-northeast-1.amazonaws.com/base/tomcat:AZ2023-J11.0.29-T7.0.76 AS build
RUN dnf install -y ant cpio java-11-amazon-corretto-devel && dnf clean all
RUN set -eu; javac_path=$(find /usr/lib/jvm -path '*java-11*' -type f -name javac | head -n 1); test -n "$javac_path"; ln -s "$(dirname "$(dirname "$javac_path")")" /opt/feature-java
ENV GRADLE_OPTS=-Dfile.encoding=UTF-8
ENV JAVA_TOOL_OPTIONS=-Dfile.encoding=UTF-8
ENV JAVA_HOME=/opt/feature-java
ENV PATH=/opt/feature-java/bin:${PATH}

COPY web/static_source/ /src/static_source/
WORKDIR /src/static_source
# Ant can finish despite missing assets; require the main JS and both stylesheets.
RUN ant -f build.xml \
	&& test -s built-results/js/jquery.js \
	&& test -s built-results/themes/porters.css \
	&& test -s built-results/themes/portersLogin.css
FROM ${RUNTIME_IMAGE}
# A clean replacement prevents deleted local files surviving from the base image.
RUN rm -rf /var/www/static /var/www/hrbc
COPY web/product/ /var/www/hrbc/
# Validate the production bootstrap and grant the runtime user its writable directories.
RUN test -s /var/www/hrbc/yii-1.1.29.f89b76/framework/yiilite.php \
	&& test -s /var/www/hrbc/htdocs/site/index.php.production \
	&& mkdir -p /var/www/hrbc/htdocs/site/assets /var/www/hrbc/files /var/www/hrbc/runtime-view \
	&& chown -R www-data:www-data /var/www/hrbc/htdocs/site/assets /var/www/hrbc/files /var/www/hrbc/runtime-view
# Include all dev-served static trees, then overlay their production build outputs.
COPY web/static_source/js/ /var/www/static/js/
COPY web/static_source/lib/ /var/www/static/lib/
COPY web/static_source/themes/ /var/www/static/themes/
COPY web/static_source/pages/ /var/www/static/pages/
COPY web/static_source/extensions/ /var/www/static/extensions/
COPY web/static_source/common/ /var/www/static/common/
COPY --from=build /src/static_source/built-results/ /var/www/static/
# Python stages this Apache config in the snapshot context; .htaccess is not enabled.
COPY web-static.conf /etc/httpd/conf.d/feature-static.conf
# Check both JS and CSS using the version emitted by the deployed PHP source.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=18 CMD version=$(head -n 1 /var/www/hrbc/systeminfo/version.txt) && curl -fsS "http://127.0.0.1/P-${version}/js/jquery.js" >/dev/null && curl -fsS "http://127.0.0.1/P-${version}/themes/porters.css" >/dev/null || exit 1