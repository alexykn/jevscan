package Config::Reader;

sub Config::Reader::read {
    my ($self, $source) = @_;
    my $text = $self->_read_manifest($source);
    my $settings = $self->_parse_manifest($text);
    $self->_store_settings($settings);
    return $settings;
}

sub _read_manifest {
    my ($self, $source) = @_;
    return $source->read;
}

sub _parse_manifest {
    my ($self, $text) = @_;
    return decode_settings($text);
}

sub _store_settings {
    my ($self, $settings) = @_;
    $self->{settings} = $settings;
}

1;
