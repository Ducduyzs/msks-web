-- Gia cố bảo mật (10/10/2026): giới hạn tần suất dùng chung giữa các tiến trình API và nhật ký sự kiện bảo mật.
--
-- rate_limit_bucket: bộ đếm cửa sổ cố định theo khóa (vd. "qa:user:<uuid>", "auth:ip:<ip>"). Lưu trong Postgres
--   để giới hạn đúng khi chạy nhiều tiến trình API; dòng cũ được worker dọn định kỳ.
-- security_event: đăng nhập vào app, đăng xuất mọi thiết bị, bật/tắt 2FA, bị chặn (CSRF, vượt tần suất, thiếu 2FA,
--   phiên đã thu hồi). Người dùng xem sự kiện của chính mình qua API; không cấp quyền đọc trực tiếp.
--
-- Cả hai bảng chỉ backend (vai trò postgres) truy cập: bật RLS, không có policy, thu hồi quyền của anon/authenticated.

create table public.rate_limit_bucket (
  key          text not null check (char_length(key) between 1 and 200),
  window_start timestamptz not null,
  hits         integer not null default 0 check (hits >= 0),
  primary key (key, window_start)
);
create index rate_limit_bucket_window_idx on public.rate_limit_bucket (window_start);

create table public.security_event (
  id         bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  user_id    uuid references auth.users (id) on delete cascade,
  event      text not null check (event in (
               'session_created', 'session_revoked', 'sessions_revoked_all', 'csrf_rejected', 'rate_limited',
               'mfa_required', 'revoked_session_used', 'mfa_enrolled', 'mfa_removed')),
  ip         text,
  user_agent text,
  detail     jsonb not null default '{}'::jsonb
);
create index security_event_user_idx on public.security_event (user_id, created_at desc);
create index security_event_created_idx on public.security_event (created_at);

alter table public.rate_limit_bucket enable row level security;
alter table public.security_event    enable row level security;
revoke all on public.rate_limit_bucket from anon, authenticated;
revoke all on public.security_event    from anon, authenticated;
