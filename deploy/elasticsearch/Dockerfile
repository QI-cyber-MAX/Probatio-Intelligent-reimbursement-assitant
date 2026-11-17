FROM docker.elastic.co/elasticsearch/elasticsearch:9.1.4@sha256:62a245d92e8d14440e1e43a1b4f08d37aef316d82f198b4f878985948aa2d1d8

ADD --checksum=sha256:0bf5c3e809212dc4646e34c9e81169007c4bc400526675d37a4fd7d6b285e2db \
    https://get.infini.cloud/elasticsearch/analysis-ik/9.1.4 /tmp/analysis-ik.zip
USER root
RUN chmod 644 /tmp/analysis-ik.zip
USER elasticsearch
RUN bin/elasticsearch-plugin install --batch analysis-smartcn \
    && bin/elasticsearch-plugin install --batch file:///tmp/analysis-ik.zip

# Preserve the entire distribution's config, including all base dictionaries.
USER root
RUN mkdir /tmp/ik-config \
    && cd /tmp/ik-config \
    && /usr/share/elasticsearch/jdk/bin/jar xf /tmp/analysis-ik.zip config \
    && cp -a config/. /usr/share/elasticsearch/config/analysis-ik/ \
    && rm -rf /tmp/ik-config /tmp/analysis-ik.zip
COPY --chown=elasticsearch:root analysis-ik/ /usr/share/elasticsearch/config/analysis-ik/
RUN chmod -R a+rX /usr/share/elasticsearch/config/analysis-ik
USER elasticsearch
