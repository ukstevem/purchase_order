conda didnt work

docker build -t po-system .
docker run -p 5050:5000 po-system
docker run -e FLASK_ENV=development -p 5050:5000 po-system