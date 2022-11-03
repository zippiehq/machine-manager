FROM ubuntu:22.04 as build-image

# Update default packages
RUN apt-get update

# Get Ubuntu packages
RUN apt-get install -y \
    build-essential \
    curl

# Get Rust
RUN curl https://sh.rustup.rs -sSf | bash -s -- -y
RUN echo 'source $HOME/.cargo/env' >> $HOME/.bashrc
ENV PATH="/root/.cargo/bin:${PATH}"

# Check cargo is visible
RUN cargo --version

# Build cartesi grpc interfaces code
COPY ./Cargo.lock /root/
COPY ./Cargo.toml /root/
COPY ./lib/grpc-interfaces /root/lib/grpc-interfaces
COPY ./cartesi-grpc-interfaces /root/cartesi-grpc-interfaces
COPY ./grpc-cartesi-machine /root/grpc-cartesi-machine
COPY ./machine-manager-server /root/machine-manager-server
COPY ./tests /root/tests
RUN cd /root/cartesi-grpc-interfaces && cargo build --release --locked

# Build grpc cartesi machine client
RUN cd /root/grpc-cartesi-machine && cargo build --release --locked

# Build machine manager server
RUN cd /root/machine-manager-server && cargo build --release --locked && cargo install --locked --force --path . --root /root/cargo

# Container final image
# ----------------------------------------------------
FROM ubuntu:22.04 as emulator-builder

RUN apt-get update && \
    DEBIAN_FRONTEND="noninteractive" apt-get install --no-install-recommends -y \
        build-essential wget git \
        libreadline-dev libboost-coroutine-dev libboost-context-dev \
        libboost-filesystem-dev libssl-dev libc-ares-dev zlib1g-dev \
        ca-certificates automake libtool patchelf cmake pkg-config lua5.3 liblua5.3-dev luarocks && \
    rm -rf /var/lib/apt/lists/*

RUN luarocks install luasocket && \
    luarocks install luasec && \
    luarocks install lpeg && \
    luarocks install dkjson

WORKDIR /usr/src/emulator
COPY machine-emulator .

RUN make -j$(nproc) dep && \
    make -j$(nproc) && \
    make install && \
    make clean && \
    rm -rf *

FROM ubuntu:22.04

RUN apt-get update && DEBIAN_FRONTEND="noninteractive" apt-get install -y \
    libboost-coroutine1.74.0 \
    libboost-context1.74.0 \
    libboost-filesystem1.74.0 \
    libreadline8 \
    openssl \
    libc-ares2 \
    zlib1g \
    ca-certificates \
    libgomp1 \
    lua5.3 \
    genext2fs \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/cartesi/bin:${PATH}"
WORKDIR /opt/cartesi
COPY --from=emulator-builder /opt/cartesi .

LABEL maintainer="Marko Atanasievski <marko.atanasievski@cartesi.io>"

ENV BASE /opt/cartesi
ENV CARTESI_IMAGE_PATH $BASE/share/images
ENV CARTESI_BIN_PATH $BASE/bin

# Install Rust and other dependencies
RUN \
    apt-get update \
    && apt-get install -y build-essential curl libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# Copy machine manager
COPY --from=build-image /root/cargo/bin/machine-manager $CARTESI_BIN_PATH/machine-manager
ENV PATH=$CARTESI_BIN_PATH:$PATH

EXPOSE 50051

## Changing directory to base
WORKDIR $BASE
CMD [ "./bin/machine-manager", "--address", "0.0.0.0", "--port", "50051","--port-checkin","50052"]
