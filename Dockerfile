FROM rust:1.78.0-slim-bullseye AS build
WORKDIR /app
RUN apt update
RUN apt install git cmake build-essential pkg-config fontconfig libfontconfig1-dev -y
RUN git clone https://github.com/unlimitedbacon/stl-thumb
RUN cd stl-thumb && cargo build --release

FROM python:3.11-alpine
WORKDIR /app
ENV STL_THUMB_PATH=/app/stl-thumb
COPY --from=build /app/stl-thumb/target/release/stl-thumb /app/stl-thumb
COPY *.py requirements.txt /app/
RUN pip install -r requirements.txt && chmod a+x /app/stl-thumb
ENTRYPOINT ["python3", "./main.py"]