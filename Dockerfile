FROM docker.io/playwright/chrome:playwright-1.49.1
LABEL maintainter="jockerCN <zh13825080826@gmail.com> https://github.com/jocker-cn"
WORKDIR /scrapper
ADD dist/main /scrapper/
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone
ENV CHROME_EXE=/ms-playwright/chromium-1129/chrome-linux/chrome
ENV CHROME_CAHCE=/scrapper/chrome_cache
ENTRYPOINT ["sh", "-c", "/scrapper/main --exe=$CHROME_EXE --cache=$CHROME_CAHCE"]