CREATE TABLE actor (
  actor_id INT NOT NULL ,
  first_name VARCHAR(45) NOT NULL,
  last_name VARCHAR(45) NOT NULL,
  last_update DATE NOT NULL,
  CONSTRAINT pk_actor PRIMARY KEY  (actor_id)
);

CREATE  INDEX idx_actor_last_name ON actor(last_name);
/
 
  --DROP SEQUENCE actor_sequence;

CREATE SEQUENCE actor_sequence;
/



CREATE OR REPLACE TRIGGER actor_before_trigger 
BEFORE INSERT ON actor FOR EACH ROW 
BEGIN
  IF (:NEW.actor_id IS NULL) THEN
    SELECT actor_sequence.nextval INTO :NEW.actor_id
    FROM DUAL;
  END IF;
  :NEW.last_update:=current_date;
END;
/

CREATE OR REPLACE TRIGGER actor_before_update
BEFORE UPDATE ON actor FOR EACH ROW 
BEGIN
  :NEW.last_update:=current_date;
END;
/


--
-- Table structure for table country
--

CREATE TABLE country (
  country_id INT NOT NULL,
  country VARCHAR(50) NOT NULL,
  last_update DATE,
  CONSTRAINT pk_country PRIMARY KEY (country_id)
);

---DROP SEQUENCE country_sequence;

CREATE SEQUENCE country_sequence;
/


CREATE OR REPLACE TRIGGER country_before_trigger
BEFORE INSERT ON country FOR EACH ROW 
BEGIN
  IF (:NEW.country_id IS NULL) THEN
    SELECT country_sequence.nextval INTO :NEW.country_id
    FROM DUAL;
