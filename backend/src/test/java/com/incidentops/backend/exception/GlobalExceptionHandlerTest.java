package com.incidentops.backend.exception;

import static org.assertj.core.api.Assertions.assertThat;

import com.incidentops.backend.dto.ErrorResponse;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.mock.http.MockHttpInputMessage;
import org.springframework.web.HttpRequestMethodNotSupportedException;

class GlobalExceptionHandlerTest {

    private final GlobalExceptionHandler handler = new GlobalExceptionHandler();

    @Test
    void malformedJsonReturnsSafeBadRequest() {
        HttpMessageNotReadableException ex = new HttpMessageNotReadableException(
                "JSON parse error: com.fasterxml.jackson.core.JsonParseException",
                new MockHttpInputMessage(new byte[0]));

        ResponseEntity<ErrorResponse> response = handler.handleUnreadableMessage(ex);

        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.BAD_REQUEST);
        assertThat(response.getBody()).isNotNull();
        assertThat(response.getBody().status()).isEqualTo(400);
        assertThat(response.getBody().message()).doesNotContain("jackson");
    }

    @Test
    void springErrorResponsePreservesHttpStatusAndAllowHeader() {
        HttpRequestMethodNotSupportedException ex =
                new HttpRequestMethodNotSupportedException("DELETE", List.of("GET", "POST"));

        ResponseEntity<ErrorResponse> response = handler.handleUnhandledException(ex);

        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.METHOD_NOT_ALLOWED);
        assertThat(response.getHeaders().getAllow()).isNotEmpty();
        assertThat(response.getBody()).isNotNull();
        assertThat(response.getBody().status()).isEqualTo(405);
    }

    @Test
    void unexpectedExceptionMasksInternalDetailsWith500() {
        RuntimeException ex = new RuntimeException("SQL failure jdbc:mysql://ops_user:secret-password@mysql:3306");

        ResponseEntity<ErrorResponse> response = handler.handleUnhandledException(ex);

        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.INTERNAL_SERVER_ERROR);
        assertThat(response.getBody()).isNotNull();
        assertThat(response.getBody().status()).isEqualTo(500);
        assertThat(response.getBody().message())
                .doesNotContain("secret-password")
                .doesNotContain("jdbc");
    }
}
