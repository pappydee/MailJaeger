# Reverse Proxy Configuration Examples

## Nginx
```nginx
server {
    listen 443 ssl http2;
    server_name mail.yourdomain.com;
    
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Caddy
```caddyfile
mail.yourdomain.com {
    reverse_proxy localhost:8000
}
```

See full examples and security checklist in the repository.
