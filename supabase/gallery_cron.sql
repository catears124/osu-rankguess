-- Run once in the Supabase SQL editor after storing these Vault secrets:
--   rankguess_site_url    https://your-production-domain.example
--   rankguess_cron_secret the same value as Vercel's CRON_SECRET
--
-- Supabase calls the endpoint every five minutes. The application keeps actual
-- gallery submissions 15 to 60 minutes apart with an atomic Postgres gate.

create extension if not exists pg_cron;
create extension if not exists pg_net;

do $$
declare
    existing_job bigint;
begin
    select jobid
      into existing_job
      from cron.job
     where jobname = 'rankguess-seed-gallery'
     limit 1;

    if existing_job is not null then
        perform cron.unschedule(existing_job);
    end if;
end
$$;

select cron.schedule(
    'rankguess-seed-gallery',
    '*/5 * * * *',
    $cron$
    select net.http_get(
        url := rtrim(
            (
                select decrypted_secret
                  from vault.decrypted_secrets
                 where name = 'rankguess_site_url'
                 limit 1
            ),
            '/'
        ) || '/api/cron/seed-gallery',
        headers := jsonb_build_object(
            'Authorization',
            'Bearer ' || (
                select decrypted_secret
                  from vault.decrypted_secrets
                 where name = 'rankguess_cron_secret'
                 limit 1
            ),
            'Accept',
            'application/json'
        ),
        timeout_milliseconds := 200000
    );
    $cron$
);

-- Diagnostics:
-- select jobid, jobname, schedule, active from cron.job where jobname = 'rankguess-seed-gallery';
-- select * from cron.job_run_details where jobid = (
--   select jobid from cron.job where jobname = 'rankguess-seed-gallery'
-- ) order by start_time desc limit 20;
-- select * from net._http_response order by created desc limit 20;
