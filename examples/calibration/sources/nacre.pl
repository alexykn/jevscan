sub decode_session {
    my ($text, $hydrate) = @_;
    my $payload = decode_json($text);
    die "user id missing" unless $payload->{userId};
    die "permissions missing" unless ref $payload->{permissions} eq "ARRAY" && @{$payload->{permissions}};
    my $session = eval { $hydrate->($payload) };
    die $@ if $@;
    return $session;
}
