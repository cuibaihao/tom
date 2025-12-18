CREATE TABLE IF NOT EXISTS roles (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '角色主键ID',
  code VARCHAR(64) NOT NULL COMMENT '角色编码，例如admin/hr/it/public',
  name VARCHAR(64) NOT NULL COMMENT '角色名称，展示给用户',
  description VARCHAR(255) NULL COMMENT '角色描述',
  is_system TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否系统内置角色：1=是 0=否',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_roles_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统角色表';

CREATE TABLE IF NOT EXISTS permissions (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '权限主键ID',
  code VARCHAR(128) NOT NULL COMMENT '权限编码，例如 leave.apply, leave.approve, kb.manage',
  name VARCHAR(128) NOT NULL COMMENT '权限名称，展示给用户',
  module VARCHAR(64) NOT NULL COMMENT '所属业务模块，例如 leave、ticket、kb',
  description VARCHAR(255) NULL COMMENT '权限描述',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_permissions_code (code),
  KEY idx_permissions_module (module)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统权限点表';

CREATE TABLE IF NOT EXISTS users (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '用户主键ID',
  username VARCHAR(64) NOT NULL COMMENT '登录用户名，全局唯一',
  email VARCHAR(255) NULL COMMENT '邮箱地址，可用于登录或找回密码',
  phone VARCHAR(32) NULL COMMENT '手机号，可选',
  password_hash VARCHAR(255) NOT NULL COMMENT '密码哈希（bcrypt_sha256等）',
  full_name VARCHAR(64) NULL COMMENT '用户姓名（展示用）',
  is_active TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否启用：1=启用 0=禁用',
  is_super_admin TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否超级管理员：1=是 0=否',
  last_login_at DATETIME NULL COMMENT '最后登录时间',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_users_username (username),
  UNIQUE KEY uk_users_email (email),
  UNIQUE KEY uk_users_phone (phone)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统用户表';

CREATE TABLE IF NOT EXISTS user_roles (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '用户-角色关系主键ID',
  user_id BIGINT UNSIGNED NOT NULL COMMENT '用户ID',
  role_id BIGINT UNSIGNED NOT NULL COMMENT '角色ID',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_user_roles_user_role (user_id, role_id),
  KEY idx_user_roles_user (user_id),
  KEY idx_user_roles_role (role_id),
  CONSTRAINT fk_user_roles_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_user_roles_role FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户与角色关联表';

CREATE TABLE IF NOT EXISTS role_permissions (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '角色-权限关系主键ID',
  role_id BIGINT UNSIGNED NOT NULL COMMENT '角色ID',
  permission_id BIGINT UNSIGNED NOT NULL COMMENT '权限ID',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_role_permissions (role_id, permission_id),
  KEY idx_role_permissions_role (role_id),
  KEY idx_role_permissions_perm (permission_id),
  CONSTRAINT fk_role_permissions_role FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE,
  CONSTRAINT fk_role_permissions_perm FOREIGN KEY (permission_id) REFERENCES permissions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='角色与权限关联表';

CREATE TABLE IF NOT EXISTS departments (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '部门主键ID',
  code VARCHAR(64) NOT NULL COMMENT '部门编码，内部唯一',
  name VARCHAR(128) NOT NULL COMMENT '部门名称',
  parent_id BIGINT UNSIGNED NULL COMMENT '上级部门ID，根部门为NULL',
  enabled TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否启用：1=启用 0=停用',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_departments_code (code),
  KEY idx_departments_parent (parent_id),
  CONSTRAINT fk_departments_parent FOREIGN KEY (parent_id) REFERENCES departments(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='组织部门表';

CREATE TABLE IF NOT EXISTS user_departments (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '用户-部门关系主键ID',
  user_id BIGINT UNSIGNED NOT NULL COMMENT '用户ID',
  department_id BIGINT UNSIGNED NOT NULL COMMENT '部门ID',
  is_primary TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否主部门：1=主部门 0=辅部门',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_user_departments (user_id, department_id),
  KEY idx_user_departments_user (user_id),
  KEY idx_user_departments_department (department_id),
  CONSTRAINT fk_user_departments_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_user_departments_dep FOREIGN KEY (department_id) REFERENCES departments(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户与部门关联表';


-- 初始化角色
INSERT IGNORE INTO roles (code, name, description, is_system)
VALUES
('public', '普通员工', '默认普通用户角色', 1),
('hr',     '人力资源', 'HR 人事角色，可审批请假等', 1),
('it',     'IT 支持', 'IT 服务台角色，可处理 IT 工单', 1),
('admin',  '系统管理员', '系统管理员角色', 1);

-- 初始化权限
INSERT IGNORE INTO permissions (code, name, module, description) VALUES
('leave.apply',      '提交请假申请',        'leave', '创建自己的请假单'),
('leave.view_self',  '查看自己的请假记录',  'leave', '查询、列表自己的请假单'),
('leave.view_all',   '查看所有人请假记录',  'leave', 'HR/管理可查看全员请假'),
('leave.cancel',     '取消自己的请假',      'leave', '撤销未审批通过的请假'),
('leave.modify',     '修改自己的请假',      'leave', '调整未审批的请假单'),
('leave.approve',    '审批请假单',          'leave', '审批通过请假申请'),
('leave.reject',     '驳回请假单',          'leave', '驳回/拒绝请假申请');

INSERT IGNORE INTO permissions (code, name, module, description) VALUES
('kb.view_public',   '查看公共知识库',      'kb',    '访问公共文档、RAG 问答'),
('kb.view_internal', '查看内部知识库',      'kb',    '访问内部/受限文档'),
('kb.manage_docs',   '管理知识库文档',      'kb',    '文档上传、删除、重建索引等');

INSERT IGNORE INTO permissions (code, name, module, description) VALUES
('ticket.create',    '创建 IT 工单',        'ticket', '提交 IT 报修/服务申请'),
('ticket.view_self', '查看自己的 IT 工单',  'ticket', '查看自己提交的工单'),
('ticket.view_all',  '查看所有 IT 工单',    'ticket', 'IT/管理员查看全量工单'),
('ticket.close',     '关闭 IT 工单',        'ticket', '标记工单已解决/关闭');

INSERT IGNORE INTO permissions (code, name, module, description) VALUES
('system.manage_users', '管理用户',          'system', '创建/修改/禁用用户'),
('system.manage_roles', '管理角色与权限',    'system', '配置角色与权限关系');

-- 绑定角色与权限
INSERT IGNORE INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r
JOIN permissions p
WHERE r.code = 'public' AND p.code IN (
  'leave.apply','leave.view_self','leave.cancel','leave.modify',
  'kb.view_public',
  'ticket.create','ticket.view_self'
);

INSERT IGNORE INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r
JOIN permissions p
WHERE r.code = 'hr' AND p.code IN (
  'leave.apply','leave.view_self','leave.view_all','leave.cancel','leave.modify','leave.approve','leave.reject',
  'kb.view_public','kb.view_internal'
);

INSERT IGNORE INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r
JOIN permissions p
WHERE r.code = 'it' AND p.code IN (
  'ticket.create','ticket.view_self','ticket.view_all','ticket.close',
  'kb.view_public','kb.view_internal'
);

-- admin 拿所有权限
INSERT IGNORE INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r
JOIN permissions p
WHERE r.code = 'admin';
