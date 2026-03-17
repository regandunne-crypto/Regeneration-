create extension if not exists pgcrypto;

create or replace function public.set_timestamp_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create table if not exists public.quiz_lecturers (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  email text not null unique,
  password_hash text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.quiz_lecturers
  add column if not exists name text,
  add column if not exists email text,
  add column if not exists password_hash text,
  add column if not exists created_at timestamptz not null default now(),
  add column if not exists updated_at timestamptz not null default now();

create unique index if not exists quiz_lecturers_email_idx
  on public.quiz_lecturers (lower(email));

create table if not exists public.quiz_tests (
  id uuid primary key default gen_random_uuid(),
  subject_code text not null,
  title text not null,
  chapter text,
  description text,
  question_count integer not null default 0,
  questions jsonb not null default '[]'::jsonb,
  created_by uuid references public.quiz_lecturers(id) on delete set null,
  updated_by uuid references public.quiz_lecturers(id) on delete set null,
  owner_name text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.quiz_tests
  add column if not exists subject_code text,
  add column if not exists title text,
  add column if not exists chapter text,
  add column if not exists description text,
  add column if not exists question_count integer not null default 0,
  add column if not exists questions jsonb not null default '[]'::jsonb,
  add column if not exists created_by uuid references public.quiz_lecturers(id) on delete set null,
  add column if not exists updated_by uuid references public.quiz_lecturers(id) on delete set null,
  add column if not exists owner_name text,
  add column if not exists created_at timestamptz not null default now(),
  add column if not exists updated_at timestamptz not null default now();

create index if not exists quiz_tests_subject_idx
  on public.quiz_tests(subject_code, updated_at desc);

create index if not exists quiz_tests_created_by_idx
  on public.quiz_tests(created_by);

create table if not exists public.quiz_test_drafts (
  id uuid primary key default gen_random_uuid(),
  lecturer_id uuid not null references public.quiz_lecturers(id) on delete cascade,
  subject_code text not null,
  title text,
  chapter text,
  description text,
  question_count integer not null default 0,
  questions jsonb not null default '[]'::jsonb,
  editing_test_id text,
  owner_name text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.quiz_test_drafts
  add column if not exists lecturer_id uuid references public.quiz_lecturers(id) on delete cascade,
  add column if not exists subject_code text,
  add column if not exists title text,
  add column if not exists chapter text,
  add column if not exists description text,
  add column if not exists question_count integer not null default 0,
  add column if not exists questions jsonb not null default '[]'::jsonb,
  add column if not exists editing_test_id text,
  add column if not exists owner_name text,
  add column if not exists created_at timestamptz not null default now(),
  add column if not exists updated_at timestamptz not null default now();

create unique index if not exists quiz_test_drafts_lecturer_subject_idx
  on public.quiz_test_drafts(lecturer_id, subject_code);

create index if not exists quiz_test_drafts_updated_idx
  on public.quiz_test_drafts(updated_at desc);

create table if not exists public.quiz_subjects (
  code text primary key,
  name text not null,
  created_by uuid references public.quiz_lecturers(id) on delete set null,
  created_at timestamptz not null default now()
);

alter table public.quiz_subjects enable row level security;

drop trigger if exists trg_quiz_lecturers_updated_at on public.quiz_lecturers;
create trigger trg_quiz_lecturers_updated_at
before update on public.quiz_lecturers
for each row execute function public.set_timestamp_updated_at();

drop trigger if exists trg_quiz_tests_updated_at on public.quiz_tests;
create trigger trg_quiz_tests_updated_at
before update on public.quiz_tests
for each row execute function public.set_timestamp_updated_at();

drop trigger if exists trg_quiz_test_drafts_updated_at on public.quiz_test_drafts;
create trigger trg_quiz_test_drafts_updated_at
before update on public.quiz_test_drafts
for each row execute function public.set_timestamp_updated_at();

alter table public.quiz_lecturers enable row level security;
alter table public.quiz_tests enable row level security;
alter table public.quiz_test_drafts enable row level security;

-- No anon/authenticated policies are created here.
-- The Render backend uses the server-side service_role key, which bypasses RLS.
