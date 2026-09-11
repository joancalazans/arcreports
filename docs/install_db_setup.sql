-- Setup inicial do banco ArcReports
CREATE DATABASE IF NOT EXISTS `reports`
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

-- Databases de conectores são criadas automaticamente pelo portal ao criar
-- cada conector — não criar aqui.

GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,
  DROP,INDEX,ALTER ON `reports`.*
  TO 'glpi_portal'@'localhost';
GRANT ALL PRIVILEGES ON `reports`.*
  TO 'portal_db_admin'@'localhost'
  WITH GRANT OPTION;
GRANT SELECT ON `reports`.*
  TO 'portal_db_user'@'localhost'
  WITH GRANT OPTION;
FLUSH PRIVILEGES;
