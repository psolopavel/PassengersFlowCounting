-- Presence / «онлайн» користувача.
-- Онлайн обчислюється як свіжість last_seen_at (heartbeat з відкритої вкладки),
-- а не зберігається булевим прапорцем. Стара колонка is_online більше не потрібна.
ALTER TABLE `users`
    ADD COLUMN `last_seen_at` DATETIME NULL DEFAULT NULL AFTER `last_login_at`;

-- Прибираємо застарілий прапорець (онлайн тепер із last_seen_at):
ALTER TABLE `users`
    DROP COLUMN `is_online`;
