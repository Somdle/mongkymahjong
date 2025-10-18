-- # DB 설정
docker exec -it <CONTAINER_NAME> bash

mysql -u root -p

CREATE DATABASE monkeymahjong CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;

CREATE USER 'monkeymahjong'@'%' IDENTIFIED BY 'monkeymahjong1324~';
GRANT ALL ON monkeymahjong.* TO 'monkeymahjong'@'%';
FLUSH PRIVILEGES;

EXIT;