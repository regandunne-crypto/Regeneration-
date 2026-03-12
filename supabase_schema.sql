create extension if not exists pgcrypto;

create table if not exists public.quiz_tests (
  id uuid primary key default gen_random_uuid(),
  subject_code text not null,
  title text not null,
  chapter text,
  description text,
  question_count integer not null default 0,
  questions jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists quiz_tests_subject_idx
  on public.quiz_tests(subject_code, updated_at desc);

create or replace function public.set_quiz_tests_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_quiz_tests_updated_at on public.quiz_tests;
create trigger trg_quiz_tests_updated_at
before update on public.quiz_tests
for each row
execute function public.set_quiz_tests_updated_at();

alter table public.quiz_tests enable row level security;
-- No anon/authenticated policies are created here.
-- The server uses the service_role key from Render, which bypasses RLS on the backend.
