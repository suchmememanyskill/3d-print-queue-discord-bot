FROM openscad/openscad:dev
WORKDIR /app
RUN apt update && apt install python3.11-venv imagemagick -y
ENV OPENSCAD_PATH=/usr/local/bin/openscad
ENV CONVERT_PATH=/usr/bin/convert
COPY *.py requirements.txt run.sh setup.sh blank.scad /app/
RUN chmod a+x run.sh && chmod a+x setup.sh
RUN /app/setup.sh
ENTRYPOINT ["./run.sh"]