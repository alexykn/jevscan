package Billing;

sub make_invoice {
    my ($id, $lines) = @_;
    die "invoice is incomplete" unless $id && ref $lines eq "ARRAY" && @$lines;
    return bless { id => $id, lines => [@$lines] }, "Billing::Invoice";
}

sub invoice_id {
    my ($self) = @_;
    return $self->{id};
}

sub make_index {
    return { entries => {} };
}

sub index_add {
    my ($index, $invoice) = @_;
    $index->{entries}{invoice_id($invoice)} = $invoice;
}

sub index_find {
    my ($index, $id) = @_;
    return $index->{entries}{$id};
}

sub settle_invoice {
    my ($id, $lines) = @_;
    my $result;
    eval {
        my $invoice = make_invoice($id, $lines);
        my $index = make_index();
        index_add($index, $invoice);
        my $stored = index_find($index, invoice_id($invoice));
        die "invoice missing after indexing" unless $stored;
        $result = { status => "settled", invoice => $stored };
    };
    return $result unless $@;
    return { status => "rejected", reason => "$@" };
}

1;
