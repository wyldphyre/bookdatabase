FROM python:3.12-slim

WORKDIR /app

# The time zone "today" and times are in: release-day notifications, dates
# added and read, log timestamps. Set here so it comes with the image rather
# than depending on the compose file on the host being updated. The base image
# has the zone data, so any name works.
ENV TZ=Australia/Perth

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p instance static/uploads

EXPOSE 5001

CMD ["gunicorn", "--bind", "0.0.0.0:5001", "--workers", "1", "--threads", "4", "--timeout", "120", "app:app"]
