FROM rust:1.78.0-slim-bullseye AS build
WORKDIR /app
RUN apt update
RUN apt install git cmake build-essential pkg-config fontconfig libfontconfig1-dev -y
RUN git clone https://github.com/unlimitedbacon/stl-thumb
RUN cd stl-thumb && cargo build --release

FROM python:3.11-slim
WORKDIR /app
RUN apt update && apt install libfreetype6 libfontconfig libx11-dev libxcursor1 libxrandr2 libxi6 libx11-xcb1 xvfb -y
ENV STL_THUMB_PATH=/app/bootstrap.sh
COPY --from=build /app/stl-thumb/target/release/stl-thumb /app/stl-thumb
COPY *.py requirements.txt bootstrap.sh bootstrap2.sh /app/
RUN pip install -r requirements.txt && chmod a+x /app/stl-thumb && chmod a+x /app/bootstrap.sh && chmod a+x /app/bootstrap2.sh
ENTRYPOINT ["python3", "./main.py"]