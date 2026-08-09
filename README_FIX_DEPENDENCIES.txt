МАНУЛ ЧАТ — PUSH FIX DEPENDENCIES
==================================

Причина ошибки:
в запущенном Docker image отсутствует библиотека pywebpush.

В requirements.txt теперь явно есть:
    pywebpush
    cryptography

Dockerfile после установки выполняет проверку:
    import pywebpush, cryptography

При успешной сборке в логе BUILD должна быть строка:
    Push dependencies: OK

ВАЖНО НА SYNOLOGY:
Недостаточно просто перезапустить контейнер.

Нужно:
1. Заменить ВСЕ файлы проекта, включая:
   - app.py
   - requirements.txt
   - Dockerfile
   - static/
2. Папку data/ НЕ удалять.
3. В Container Manager остановить проект.
4. Выполнить именно Build / Rebuild проекта.
5. Если Synology всё равно использует старый image:
   удалить старый image manul-chat и снова Build проекта.
   Папка data при этом сохраняется, потому что она bind-mounted.
6. После запуска проверить:
   Манул Чат: 2026.08.09-pwa-push1-fixdeps

Если при BUILD нет строки:
    Push dependencies: OK
значит используется старый Dockerfile / старый build context.
