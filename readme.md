conda didnt work

docker build -t po-system .

<!-- old -->
<!-- docker run -p 5050:5000 po-system

or

docker run -e FLASK_ENV=development -p 5050:5000 po-system -->


docker compose -f docker-compose.dev.yml up --build



http://127.0.0.1:5050/po-list