FROM alpine:3.22
RUN apk add --no-cache bash coreutils findutils ffmpeg
COPY convert.sh /usr/local/bin/convert.sh
ENTRYPOINT ["/usr/local/bin/convert.sh"]
