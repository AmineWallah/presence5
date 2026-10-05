#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <poll.h>
#include <time.h>
#include <stdbool.h>

typedef struct notify_request {
    char useless1[45];
    char message[3075];
} notify_request_t;

int sceSystemServiceGetAppIdOfRunningBigApp(void);
int sceSystemServiceGetAppTitleId(int app_id, char *title_id);
int sceKernelSendNotificationRequest(int, notify_request_t*, size_t, int);

int notify(const char* message)
{
    notify_request_t req;

    bzero(&req, sizeof req);
    strncpy(req.message, message, sizeof req.message);

    return sceKernelSendNotificationRequest(0, &req, sizeof req, 0);
}

int main(void) {
    notify("Payload sent");
    char line[64]; // Holds finished message to send

    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd == -1) {
        notify("Socket failed");
        return 1;
    }

    struct sockaddr_in server_addr = {0};
    server_addr.sin_family = AF_INET;
    server_addr.sin_port = htons(8000);
    server_addr.sin_addr.s_addr = htonl(INADDR_ANY);

    int opt = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    // Server initialization
    if (bind(fd, (struct sockaddr *)&server_addr, sizeof(server_addr)) != 0) {
        notify("Bind failed");
        return 1;
    }

    if (listen(fd, 1) != 0) {
        notify("Listen failed");
        return 1;
    }
    notify("Server initialized");



    // Game detection part
    char current[32] = "";
    time_t start_sec = 0;
    struct timespec now;

    struct pollfd pfd = {0};
    pfd.fd = fd;
    pfd.events = POLLIN;
    int running = 1;
    while (running) {
        char seen[32] = "";
        int bigAppId = sceSystemServiceGetAppIdOfRunningBigApp();

        if (bigAppId >= 0) {
            if (sceSystemServiceGetAppTitleId(bigAppId, seen) != 0) {
                notify("Failed to get title id");
                seen[0] = '\0';
            }
        }

        if (strcmp(seen, current) != 0) {
            strcpy(current, seen);
            clock_gettime(CLOCK_MONOTONIC, &now);
            start_sec = now.tv_sec;
        }

        int ready = poll(&pfd, 1, 2000);
        if (ready <= 0 || !(pfd.revents & POLLIN)) {
            continue;
        }

        // Last step
        int client = accept(fd, NULL, NULL);
        if (client < 0) {
            notify("Accept failed");
            continue;
        }

        if (current[0] == '\0') {
            snprintf(line, sizeof(line), "IDLE\n");
        } else {
            clock_gettime(CLOCK_MONOTONIC, &now);
            long elapsed = (long)(now.tv_sec - start_sec);
            snprintf(line, sizeof(line), "GAME %s %ld\n", current, elapsed);
        }

        send(client, line, strlen(line), MSG_NOSIGNAL);

        struct pollfd cfd = {0};
        cfd.fd = client;
        cfd.events = POLLIN;

        if (poll(&cfd, 1, 200) > 0 && (cfd.revents & POLLIN)) {
            char cmd[16];
            ssize_t n = recv(client, cmd, sizeof(cmd) - 1, 0);
            if (n > 0) {
                cmd[n] = '\0';
                if (strncmp(cmd, "QUIT", 4) == 0) {
                    running = 0;

                } else if (strncmp(cmd, "PARAM", 5) == 0 && current[0] != '\0') {
                    char path[128];
                    char buf[1024];

                    snprintf(path, sizeof(path), "/user/appmeta/%s/param.json", current);
                    FILE* f = fopen(path, "rb");
                    size_t n_file;

                    if (f != NULL) {
                        while ((n_file = fread(buf, 1, sizeof(buf), f)) > 0) {
                            send(client, buf, n_file, MSG_NOSIGNAL);
                        }
                        fclose(f);
                    }

                }
            }
        }

        close(client);
    }

    close(fd);
    notify("Server closed");

    return 0;
}