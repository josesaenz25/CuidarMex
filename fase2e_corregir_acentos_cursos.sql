-- =====================================================================
-- CuidarMéx · Corrige los acentos de los cursos de Capacitación inteligente
-- (los textos se guardaron con otra codificación en Windows y se veían como
--  "Movilizaci¢n", "emp tica"). Es seguro repetirlo.
-- Ejecutar con:  $env:PGCLIENTENCODING="UTF8"  antes de psql.
-- =====================================================================
BEGIN;

UPDATE training_courses SET nombre = 'Movilización segura'
 WHERE nombre ILIKE 'Movilizaci%segura';
UPDATE training_courses SET nombre = 'Atención a personas con discapacidad'
 WHERE nombre ILIKE 'Atenci%personas con discapacidad';
UPDATE training_courses SET nombre = 'Comunicación empática'
 WHERE nombre ILIKE 'Comunicaci%emp%tica';
UPDATE training_courses SET nombre = 'Primeros auxilios'
 WHERE nombre ILIKE 'Primeros auxilios%';
UPDATE training_courses SET nombre = 'Cuidado del adulto mayor'
 WHERE nombre ILIKE 'Cuidado del adulto mayor%';

UPDATE training_courses SET categoria = 'Inclusión'
 WHERE categoria ILIKE 'Inclusi%';

-- Revisión: deben verse con acentos correctos
SELECT id, nombre, categoria FROM training_courses ORDER BY id;
COMMIT;
