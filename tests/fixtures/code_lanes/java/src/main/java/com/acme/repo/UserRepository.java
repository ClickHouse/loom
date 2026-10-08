package com.acme.repo;

import java.sql.Connection;
import java.util.List;
import com.acme.model.User;
import org.postgresql.Driver;
import org.springframework.jdbc.core.JdbcTemplate;

/** Synthetic Spring / JDBC repository: SQL as client arguments,
 *  annotation values, a text block, a concatenation and a String.format template. */
public class UserRepository {
    private final JdbcTemplate jdbc;

    public UserRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @Query("SELECT u FROM User u WHERE u.email = :email")
    User findByEmail(String email);

    @Select("SELECT id, name FROM users WHERE id = #{id}")
    User byId(int id);

    public List<User> all() {
        return jdbc.query("SELECT id, name FROM users ORDER BY id", MAPPER);
    }

    public int rename(long id, String name) {
        String sql = """
            UPDATE users
            SET name = ?
            WHERE id = ?
            """;
        jdbc.update(sql, name, id);
        return jdbc.update("DELETE FROM sessions WHERE user_id = " + id);
    }

    public void migrate(Connection c) throws Exception {
        try (var st = c.createStatement()) {
            st.execute("CREATE TABLE IF NOT EXISTS sessions (id BIGSERIAL PRIMARY KEY, user_id BIGINT, expires TIMESTAMPTZ)");
            st.execute(String.format("INSERT INTO %s (user_id) VALUES (?)", "sessions"));
        }
        String label = "Update available";
        String hint = "Select a user first";
    }
}
