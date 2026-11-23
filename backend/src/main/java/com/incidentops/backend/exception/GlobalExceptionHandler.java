package com.incidentops.backend.exception;

import com.incidentops.backend.dto.ErrorResponse;
import java.time.LocalDateTime;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

@RestControllerAdvice
public class GlobalExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

    @ExceptionHandler(ResourceNotFoundException.class)
    public ResponseEntity<ErrorResponse> handleNotFound(ResourceNotFoundException ex) {
        return buildResponse(HttpStatus.NOT_FOUND, ex.getMessage());
    }

    @ExceptionHandler(ConflictException.class)
    public ResponseEntity<ErrorResponse> handleConflict(ConflictException ex) {
        return buildResponse(HttpStatus.CONFLICT, ex.getMessage());
    }

    @ExceptionHandler(HttpMessageNotReadableException.class)
    public ResponseEntity<ErrorResponse> handleUnreadableMessage(HttpMessageNotReadableException ex) {
        return buildResponse(HttpStatus.BAD_REQUEST, "요청 본문 JSON 형식이 올바르지 않습니다.");
    }

    @ExceptionHandler({MethodArgumentNotValidException.class, IllegalArgumentException.class})
    public ResponseEntity<ErrorResponse> handleBadRequest(Exception ex) {
        String message = ex instanceof MethodArgumentNotValidException validEx
                && validEx.getBindingResult().getFieldError() != null
                ? validEx.getBindingResult().getFieldError().getDefaultMessage()
                : ex.getMessage();
        return buildResponse(HttpStatus.BAD_REQUEST, message);
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<ErrorResponse> handleUnhandledException(Exception ex) {
        if (ex instanceof org.springframework.web.ErrorResponse springError) {
            int rawCode = springError.getStatusCode().value();
            HttpStatus resolved = HttpStatus.resolve(rawCode);
            String reason = resolved != null ? resolved.getReasonPhrase() : "Error";
            String message = springError.getStatusCode().is5xxServerError() ? "서버 내부 오류가 발생했습니다." : reason;
            ErrorResponse body = new ErrorResponse(LocalDateTime.now(), rawCode, reason, message);
            return ResponseEntity.status(rawCode)
                    .headers(springError.getHeaders())
                    .body(body);
        }
        log.error("처리되지 않은 서버 오류 발생", ex);
        return buildResponse(HttpStatus.INTERNAL_SERVER_ERROR, "서버 내부 오류가 발생했습니다.");
    }

    private ResponseEntity<ErrorResponse> buildResponse(HttpStatus status, String message) {
        ErrorResponse body = new ErrorResponse(
                LocalDateTime.now(),
                status.value(),
                status.getReasonPhrase(),
                message
        );
        return ResponseEntity.status(status).body(body);
    }
}
