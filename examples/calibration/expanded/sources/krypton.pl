package Api;

sub Api::set_name {
    my ($self, $name) = @_;
    my $request = { field => "name", value => $name };
    return $self->_shape_request($request);
}

sub _shape_request {
    my ($self, $request) = @_;
    my $payload = { key => $request->{field}, data => $request->{value} };
    return $self->_submit($payload);
}

sub _submit {
    my ($self, $payload) = @_;
    return $self->{store}->set_name($payload->{data});
}

1;
