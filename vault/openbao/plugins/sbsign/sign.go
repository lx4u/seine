package main

import (
	"context"
	"encoding/base64"
	"time"

	"github.com/openbao/openbao/sdk/v2/framework"
	"github.com/openbao/openbao/sdk/v2/logical"
)

// Signs with an explicit creation time: signatures embed it, so only
// a pinned clock keeps rebuilds byte-identical. Without one the time
// is now, like sbsign unwrapped in faketime.
func handleSign(ctx context.Context, req *logical.Request, data *framework.FieldData) (*logical.Response, error) {
	name := data.Get("name").(string)
	pair, err := loadKey(ctx, req.Storage, name)
	if err != nil {
		return nil, err
	}
	encoded, _ := data.Get("pe_base64").(string)
	peBytes, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil || encoded == "" {
		return logical.ErrorResponse("pe_base64 shall be base64"), nil
	}
	signingTime := time.Now()
	if stamp, _ := data.Get("signing_time").(string); stamp != "" {
		signingTime, err = time.Parse(time.RFC3339, stamp)
		if err != nil {
			return logical.ErrorResponse("signing_time shall be RFC3339"), nil
		}
	}
	signed, err := signPE(peBytes, pair.Key, pair.Cert, signingTime)
	if err != nil {
		return nil, logical.CodedError(400, err.Error())
	}
	return &logical.Response{
		Data: map[string]interface{}{
			"signed_pe_base64": base64.StdEncoding.EncodeToString(signed),
		},
	}, nil
}

// Builds a signed UEFI variable update (EFI_VARIABLE_AUTHENTICATION_2,
// what 'efi-updatevar -f' expects) -- same explicit-timestamp shape as
// handleSign, for the same reproducibility reason.
func handleSignVar(ctx context.Context, req *logical.Request, data *framework.FieldData) (*logical.Response, error) {
	name := data.Get("name").(string)
	pair, err := loadKey(ctx, req.Storage, name)
	if err != nil {
		return nil, err
	}
	varName, _ := data.Get("var").(string)
	guid, _ := data.Get("guid").(string)
	if varName == "" || guid == "" {
		return logical.ErrorResponse("'var' and 'guid' are required"), nil
	}
	encoded, _ := data.Get("esl_base64").(string)
	esl, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil || encoded == "" {
		return logical.ErrorResponse("esl_base64 shall be base64"), nil
	}
	attributes := uint32(defaultVariableAttributes)
	if raw, ok := data.GetOk("attributes"); ok {
		attributes = uint32(raw.(int))
	}
	signingTime := time.Now()
	if stamp, _ := data.Get("signing_time").(string); stamp != "" {
		signingTime, err = time.Parse(time.RFC3339, stamp)
		if err != nil {
			return logical.ErrorResponse("signing_time shall be RFC3339"), nil
		}
	}
	auth, err := signVariable(pair, varName, guid, attributes, esl, signingTime)
	if err != nil {
		return nil, logical.CodedError(400, err.Error())
	}
	return &logical.Response{
		Data: map[string]interface{}{
			"auth_base64": base64.StdEncoding.EncodeToString(auth),
		},
	}, nil
}
