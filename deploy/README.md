# Raspberry Pi deployment

The application runs on `192.168.1.61`, Traefik and Authelia on `192.168.1.60`, and the existing PostgreSQL database on `192.168.1.60:5432`. The public address is https://coding.home.nextnet.top.

## Build and publish

Install `deploy/coding.sh` as `/home/nherbaut/coding.sh` on `.61` and make it executable. Running it clones `git@github.com:nherbaut/coding.git` into `~/coding` when needed, updates main with a fast-forward pull, and publishes `docker.io/nherbaut/coding:arm64` plus an immutable `<commit>-arm64` tag. It builds explicitly for `linux/arm64` using the existing Docker Hub login. `CODING_CHECKOUT`, `CODING_REPOSITORY`, `CODING_IMAGE`, and `CODING_TAG` can override the defaults. Local checkout changes cause a clear failure before building.

## Application service

Add the `coding` service from `compose-service.yaml` to `/home/nherbaut/server/docker-compose.yaml`. It uses the existing database and publishes `192.168.1.61:8147`. Copy `coding.env.example` to `/home/nherbaut/server/coding.env`, set real secrets, and set permissions to 600. This environment file is outside the Git checkout. Migrations run before Gunicorn starts. Other services in the server stack are independent of this deployment.

```sh
cd /home/nherbaut/server
docker compose pull coding
docker compose up -d --no-deps coding
docker compose ps coding
```

## Traefik and administrator authentication

Render `traefik-coding.yaml` with `__CODING_PROXY_SECRET__` replaced by the same `AUTHELIA_PROXY_SECRET` stored in the application environment. Store the result as `/srv/stacks/traefik/coding.yaml` on `.60` with permissions 600. Traefik watches this directory. Keep the rendered file outside Git.

In Authelia's access-control rules, place these rules **before** the existing wildcard `home.nextnet.top` rule.

```yaml
- domain: coding.home.nextnet.top
  policy: one_factor
  subject: group:coding-admins
- domain: coding.home.nextnet.top
  policy: deny
```

Create the LDAP group `coding-admins` and assign the intended administrators through LLDAP. Membership in `home` alone does not grant administration of this application. Validate Authelia configuration before restarting its container. Existing Authelia deprecation warnings are independent of this integration.

The admin routes `/`, `/admin*`, `/review*` and `/exports*` use Authelia ForwardAuth. Traefik removes incoming identity headers, copies authenticated user/group response headers, then adds a shared proxy secret. Django checks that secret and the immediate proxy address, and evaluates the exact LDAP group on every request. It creates a local staff identity for audit records, with an unusable password. Administrator password sessions do not grant access in Authelia mode. Removing group membership takes effect when Authelia refreshes its LDAP identity information, and Django requires the group on every request it receives.

Private coder links, coder APIs, static assets and `/healthz/` use the independent public router. They retain the application's private-token authorization and CSRF checks. Coder requests receive no administrator identity from the proxy.

The proxy trusts only `192.168.1.60/32` as configured in the environment. If an additional proxy is introduced, explicitly update the trusted peer addresses. Never use a universal trusted range.

## Maintenance

To change the database password, update PostgreSQL's `coding` role and `/home/nherbaut/server/coding.env`, then recreate only the coding service. To rotate the proxy secret, update both the rendered Traefik file and `coding.env`, then recreate the coding service. Keep `DJANGO_SECRET_KEY` stable across routine image updates so that existing private-link sessions continue working.

Use `pg_dump` on the existing PostgreSQL host to back up the `coding` database. Back up the protected environment file separately. Database snapshots include corpus text, codebooks, private-link token hashes, annotations, immutable submissions and audit records.

Integration references are the [Authelia Traefik documentation](https://www.authelia.com/integration/proxies/traefik/) and [Django remote-user authentication documentation](https://docs.djangoproject.com/en/5.2/howto/auth-remote-user/).
