FROM clamav/clamav:1.5.3@sha256:7f5389ccaa2368c383fa80e167ccfe44348d71e685f926fce4755eed1757673a

RUN addgroup -g 10001 ira-runtime \
    && addgroup clamav ira-runtime \
    && mkdir -p /run/clamav \
    && chown root:ira-runtime /run/clamav \
    && chmod 2770 /run/clamav
