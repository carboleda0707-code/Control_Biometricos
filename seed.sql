-- Inicialización de las 5 Empacadoras de Banano
INSERT INTO empacadoras (codigo, nombre, activa) VALUES
('EMP01', 'Empacadora Banano Hacienda San José', TRUE),
('EMP02', 'Empacadora Banano La Providencia', TRUE),
('EMP03', 'Empacadora Banano El Triunfo', TRUE),
('EMP04', 'Empacadora Banano Doña Inés', TRUE),
('EMP05', 'Empacadora Banano Los Almendros', TRUE)
ON CONFLICT (codigo) DO NOTHING;