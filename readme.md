conda didnt work

docker build -t po-system .


docker run -p 5050:5000 po-system

or

docker run -e FLASK_ENV=development -p 5050:5000 po-system


http://127.0.0.1:5050/po-list