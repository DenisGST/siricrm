# Удалить/очистить все данные Докера (контейнеры, образы, тома и сети)

## Одной строкой

```bash
docker stop $(docker ps -qa) && docker rm $(docker ps -qa) && docker rmi -f $(docker images -qa) && docker volume rm $(docker volume ls -q) && docker network rm $(docker network ls -q)
```

## Описание команд

### :thumbsdown: Остановка всех контейнеров

```bash
docker stop $(docker ps -qa)
```

### :fist: Удаление всех контейнеров

```bash
docker rm $(docker ps -qa)
```

### :v: Удаление всех образов

```bash
docker rmi -f $(docker images -qa)
```

### :wave: Удаление всех томов

```bash
docker volume rm $(docker volume ls -q)
```

### :hand: Удаление всех сетей

```bash
docker network rm $(docker network ls -q)
```

> Ваша инсталяция должна быть чиста :blush:

## :point_down: Следующие команды не должны выводить какие-либо элементы:

```bash
docker ps -a
```

```bash
docker images -a 
```

```bash
docker volume ls
```

## :point_right: Следующая команда показывает только сети по умолчанию:

```bash
docker network ls
```

## :: Следующая команда удаляет неиспользуемые образы

```bash
docker system prune
```